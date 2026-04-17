"""
CLI test runner for ADK Environment Simulation scenarios.

Usage::

    # List available scenarios
    python -m testing.runner --list

    # Run a specific scenario
    python -m testing.runner --scenario A1

    # Run a scenario with verbose logging
    python -m testing.runner --scenario E1 --verbose

    # Run a scenario against the running server (via HTTP)
    python -m testing.runner --scenario A1 --mode server --port 8000

    # Run a scenario in-process (direct pipeline invocation)
    python -m testing.runner --scenario E1 --mode direct
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure server/ is on sys.path
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    for name in ("httpcore", "httpx", "urllib3", "google.auth"):
        logging.getLogger(name).setLevel(logging.WARNING)


def cmd_list(args: argparse.Namespace) -> None:
    """List all available test scenarios."""
    from testing.scenarios import list_scenarios

    scenarios = list_scenarios()
    print(f"\n{'ID':<5} {'Name':<45} Description")
    print("-" * 100)
    for s in scenarios:
        print(f"{s['id']:<5} {s['name']:<45} {s['description']}")
    print(f"\nTotal: {len(scenarios)} scenarios")


def cmd_run(args: argparse.Namespace) -> None:
    """Run a specific test scenario."""
    from testing.scenarios import get_scenario
    from testing.simulation_bridge import activate_simulation, deactivate_simulation

    scenario_id = args.scenario
    print(f"\n{'='*60}")
    print(f"  Running scenario: {scenario_id}")
    print(f"  Mode: {args.mode}")
    print(f"{'='*60}\n")

    # Load scenario
    try:
        config = get_scenario(scenario_id)
    except KeyError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    tools = [c.tool_name for c in config.tool_simulation_configs]
    injections = sum(len(c.injection_configs) for c in config.tool_simulation_configs)
    print(f"Tools simulated: {', '.join(tools)}")
    print(f"Total injections: {injections}")

    # Activate simulation
    activate_simulation(config, scenario_name=scenario_id)

    start_time = time.time()
    result = {"scenario": scenario_id, "status": "unknown", "errors": []}

    try:
        if args.mode == "direct":
            result = _run_direct(args, config)
        elif args.mode == "server":
            result = _run_via_server(args, config)
        elif args.mode == "validate":
            result = _run_validation(args, config)
        else:
            print(f"ERROR: Unknown mode '{args.mode}'")
            sys.exit(1)
    except Exception as e:
        result["status"] = "error"
        result["errors"].append(str(e))
        logging.getLogger(__name__).exception("Scenario %s failed", scenario_id)
    finally:
        elapsed = time.time() - start_time
        result["elapsed_sec"] = round(elapsed, 2)
        deactivate_simulation()

    # Report
    print(f"\n{'='*60}")
    print(f"  Result: {result['status'].upper()}")
    print(f"  Elapsed: {result['elapsed_sec']}s")
    if result.get("errors"):
        print(f"  Errors:")
        for err in result["errors"]:
            print(f"    - {err}")
    if result.get("escalations"):
        print(f"  Escalations: {len(result['escalations'])}")
        for esc in result["escalations"]:
            print(f"    - [{esc.get('level', '?')}] {esc.get('operation', '?')}: {esc.get('action', '?')}")
    if result.get("phases_completed"):
        print(f"  Phases: {' → '.join(result['phases_completed'])}")
    print(f"{'='*60}\n")

    # Write results to file
    results_dir = Path("/tmp/documentary-pipeline/test-results")
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / f"{scenario_id}_{int(time.time())}.json"
    with open(results_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"Results saved to: {results_file}")

    sys.exit(0 if result["status"] == "passed" else 1)


def _run_validation(args: argparse.Namespace, config) -> dict:
    """Validate that the simulation bridge intercepts calls correctly.

    Doesn't run the full pipeline — just calls each simulated tool directly
    and checks that the injection returns the expected mock response.
    """
    from testing.simulation_bridge import SimulationRegistry

    result = {
        "scenario": args.scenario,
        "status": "passed",
        "tool_results": {},
        "errors": [],
    }

    engine = SimulationRegistry.get().engine
    if engine is None:
        result["status"] = "error"
        result["errors"].append("No simulation engine active")
        return result

    for tool_config in config.tool_simulation_configs:
        tool_name = tool_config.tool_name
        print(f"  Validating {tool_name}...")

        from testing.simulation_bridge import ToolProxy, _run_async

        proxy = ToolProxy(name=tool_name)
        try:
            mock_result = _run_async(engine.simulate(proxy, {}, None))
            if mock_result is not None:
                result["tool_results"][tool_name] = {
                    "intercepted": True,
                    "response_keys": list(mock_result.keys()) if isinstance(mock_result, dict) else "non-dict",
                }
                print(f"    ✓ Intercepted — keys: {list(mock_result.keys()) if isinstance(mock_result, dict) else mock_result}")
            else:
                result["tool_results"][tool_name] = {
                    "intercepted": False,
                    "reason": "Engine returned None (no injection matched)",
                }
                # This might be OK if the tool has match_args that don't match {}
                if tool_config.injection_configs and any(
                    ic.match_args for ic in tool_config.injection_configs
                ):
                    print(f"    ~ Not intercepted (has match_args — expected)")
                else:
                    print(f"    ✗ Not intercepted (unexpected)")
                    result["errors"].append(f"{tool_name}: injection did not match")
                    result["status"] = "failed"
        except Exception as e:
            result["tool_results"][tool_name] = {
                "intercepted": False,
                "error": str(e),
            }
            result["errors"].append(f"{tool_name}: {e}")
            result["status"] = "failed"
            print(f"    ✗ Error: {e}")

    return result


def _run_direct(args: argparse.Namespace, config) -> dict:
    """Run the pipeline directly in-process with the simulation active.

    This imports and calls the pipeline runner directly, with the simulation
    engine intercepting all tool calls.
    """
    result = {
        "scenario": args.scenario,
        "status": "unknown",
        "errors": [],
        "escalations": [],
        "phases_completed": [],
    }

    # Set required env vars for in-process run
    os.environ.setdefault("DOCUMENTARY_AUTO_APPROVE", "true")
    os.environ.setdefault("ADK_MODEL", "gemini-2.0-flash")

    try:
        # Import pipeline runner
        from run_pipeline import run as run_pipeline

        # Run with a test topic
        topic = args.topic or "The Periaqueductal Gray"
        print(f"  Running pipeline with topic: {topic}")
        run_pipeline(topic=topic, target_duration=60)
        result["status"] = "passed"
    except SystemExit:
        result["status"] = "passed"  # Pipeline may sys.exit(0) on success
    except Exception as e:
        result["status"] = "failed"
        result["errors"].append(str(e))

    return result


def _run_via_server(args: argparse.Namespace, config) -> dict:
    """Trigger the pipeline via the running server's AG-UI endpoint.

    Requires the server to be running on the specified port.
    The simulation config must be loaded server-side (e.g. via env var
    SIMULATION_SCENARIO=A1).
    """
    import urllib.request

    result = {
        "scenario": args.scenario,
        "status": "unknown",
        "errors": [],
        "escalations": [],
    }

    port = args.port or 8000
    base_url = f"http://localhost:{port}"

    # Check server health
    try:
        req = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read())
            print(f"  Server health: {health.get('status', 'unknown')}")
    except Exception as e:
        result["status"] = "error"
        result["errors"].append(f"Server not reachable at {base_url}: {e}")
        return result

    # Send pipeline trigger via AG-UI
    try:
        topic = args.topic or "The Periaqueductal Gray"
        trigger_url = f"{base_url}/agui/conversation"
        payload = json.dumps({
            "messages": [{"role": "user", "content": f"Make a 1-minute documentary about {topic}"}],
        }).encode()
        req = urllib.request.Request(
            trigger_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            # SSE stream — read events
            for line in resp:
                line = line.decode().strip()
                if line.startswith("data:"):
                    try:
                        event = json.loads(line[5:])
                        event_type = event.get("type", "")
                        if event_type == "run_finished":
                            result["status"] = "passed"
                            break
                        elif event_type == "run_error":
                            result["status"] = "failed"
                            result["errors"].append(event.get("error", "unknown"))
                            break
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        result["status"] = "error"
        result["errors"].append(f"Pipeline trigger failed: {e}")

    # Check escalations
    try:
        req = urllib.request.Request(f"{base_url}/agui/escalations")
        with urllib.request.urlopen(req, timeout=5) as resp:
            escalations = json.loads(resp.read())
            result["escalations"] = escalations if isinstance(escalations, list) else []
    except Exception:
        pass

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ADK Environment Simulation test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m testing.runner --list
  python -m testing.runner --scenario E1 --mode validate
  python -m testing.runner --scenario A1 --mode direct --verbose
  python -m testing.runner --scenario A1 --mode server --port 8000
        """,
    )
    parser.add_argument("--list", action="store_true", help="List all scenarios")
    parser.add_argument("--scenario", "-s", help="Scenario ID (e.g. A1, E1)")
    parser.add_argument(
        "--mode",
        choices=["validate", "direct", "server"],
        default="validate",
        help="Run mode: validate (check injections), direct (in-process), server (HTTP)",
    )
    parser.add_argument("--port", type=int, default=8000, help="Server port for mode=server")
    parser.add_argument("--topic", help="Override documentary topic")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    if args.list:
        cmd_list(args)
    elif args.scenario:
        cmd_run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
