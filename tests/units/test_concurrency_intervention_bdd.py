import os
import sys
import asyncio
import signal
import inspect
import pytest
import httpx
from pathlib import Path
from unittest.mock import patch

# Inject patched version of pytest-bdd scenario runner to support async step definitions
import pytest_bdd.scenario
from pytest_bdd.scenario import (
    _execute_scenario,
    collect_example_parametrizations,
    get_step_function,
    parse_step_arguments,
    STEP_ARGUMENT_DATATABLE,
    STEP_ARGUMENT_DOCSTRING,
)
from pytest_bdd.utils import get_required_args, CONFIG_STACK
from pytest_bdd.compat import inject_fixture
from _pytest.fixtures import call_fixture_func

async def async_execute_step_function(request, scenario, step, context) -> None:
    __tracebackhide__ = True

    from inspect import signature
    func_sig = signature(context.step_func)

    kw = {
        "request": request,
        "feature": scenario.feature,
        "scenario": scenario,
        "step": step,
        "step_func": context.step_func,
        "step_func_args": {},
    }
    request.config.hook.pytest_bdd_before_step(**kw)

    try:
        parsed_args = parse_step_arguments(step=step, context=context)
        kwargs = {k: v for k, v in parsed_args.items() if k in func_sig.parameters}

        if STEP_ARGUMENT_DATATABLE in func_sig.parameters and step.datatable is not None:
            kwargs[STEP_ARGUMENT_DATATABLE] = step.datatable.raw()
        if STEP_ARGUMENT_DOCSTRING in func_sig.parameters and step.docstring is not None:
            kwargs[STEP_ARGUMENT_DOCSTRING] = step.docstring

        # Fill the missing arguments requesting the fixture values
        for arg in get_required_args(context.step_func):
            if arg not in kwargs:
                val = request.getfixturevalue(arg)
                if inspect.iscoroutine(val):
                    val = await val
                kwargs[arg] = val

        kw["step_func_args"] = kwargs
        request.config.hook.pytest_bdd_before_step_call(**kw)

        # Execute the step
        if inspect.iscoroutinefunction(context.step_func):
            return_value = await context.step_func(**kwargs)
        else:
            return_value = call_fixture_func(fixturefunc=context.step_func, request=request, kwargs=kwargs)

    except Exception as exception:
        request.config.hook.pytest_bdd_step_error(exception=exception, **kw)
        raise

    if context.target_fixture is not None:
        inject_fixture(request, context.target_fixture, return_value)

    request.config.hook.pytest_bdd_after_step(**kw)

async def async_execute_scenario(feature, scenario, request) -> None:
    __tracebackhide__ = True
    request.config.hook.pytest_bdd_before_scenario(request=request, feature=feature, scenario=scenario)

    try:
        for step in scenario.steps:
            step_func_context = get_step_function(request=request, step=step)
            if step_func_context is None:
                from pytest_bdd.exceptions import StepDefinitionNotFoundError
                exc = StepDefinitionNotFoundError(
                    f"Step definition is not found: {step}. "
                    f'Line {step.line_number} in scenario "{scenario.name}" in the feature "{scenario.feature.filename}"'
                )
                request.config.hook.pytest_bdd_step_func_lookup_error(
                    request=request, feature=feature, scenario=scenario, step=step, exception=exc
                )
                raise exc
            await async_execute_step_function(request, scenario, step, step_func_context)
    finally:
        request.config.hook.pytest_bdd_after_scenario(request=request, feature=feature, scenario=scenario)

def patched_get_scenario_decorator(feature, feature_name, templated_scenario, scenario_name):
    def decorator(*args):
        if not args:
            raise Exception("scenario function can only be used as a decorator.")
        [fn] = args
        func_args = get_required_args(fn)

        is_async = inspect.iscoroutinefunction(fn)
        print(f"\nDEBUG_DECORATOR: fn={fn.__name__}, is_async={is_async}\n")

        if is_async:
            async def scenario_wrapper(request, _pytest_bdd_example):
                __tracebackhide__ = True
                scenario = templated_scenario.render(_pytest_bdd_example)
                await async_execute_scenario(feature, scenario, request)
                fixture_values = []
                for arg in func_args:
                    val = request.getfixturevalue(arg)
                    if inspect.iscoroutine(val):
                        val = await val
                    fixture_values.append(val)
                return await fn(*fixture_values)
        else:
            def scenario_wrapper(request, _pytest_bdd_example):
                __tracebackhide__ = True
                scenario = templated_scenario.render(_pytest_bdd_example)
                _execute_scenario(feature, scenario, request)
                fixture_values = [request.getfixturevalue(arg) for arg in func_args]
                return fn(*fixture_values)

        if func_args:
            scenario_wrapper = pytest.mark.usefixtures(*func_args)(scenario_wrapper)

        example_parametrizations = collect_example_parametrizations(templated_scenario)
        if example_parametrizations is not None:
            scenario_wrapper = pytest.mark.parametrize(
                "_pytest_bdd_example",
                example_parametrizations,
            )(scenario_wrapper)

        rule_tags = set() if templated_scenario.rule is None else templated_scenario.rule.tags
        for tag in templated_scenario.tags | feature.tags | rule_tags:
            config = CONFIG_STACK[-1]
            config.hook.pytest_bdd_apply_tag(tag=tag, function=scenario_wrapper)

        scenario_wrapper.__doc__ = f"{feature_name}: {scenario_name}"
        scenario_wrapper.__scenario__ = templated_scenario
        return scenario_wrapper

    return decorator

# Apply the monkeypatch to the actual module object in sys.modules
import sys
scenario_module = sys.modules['pytest_bdd.scenario']
scenario_module._get_scenario_decorator = patched_get_scenario_decorator

# Now import bdd decorators
from pytest_bdd import scenario, given, when, then, parsers

# Ensure server is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import make_agent_app, bash_command

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.fixture
def test_context():
    return {
        "turn_entered_event": asyncio.Event(),
        "turn_finish_event": asyncio.Event(),
        "first_post_task": None,
        "first_post_response": None,
        "second_post_response": None,
        "health_response": None,
        "put_response": None,
        "cancelled_event": asyncio.Event(),
        "bash_proc": None,
        "bash_pgid": None,
    }

@pytest.fixture
def mock_agent_turn(test_context):
    async def dynamic_mock(*args, **kwargs):
        if "execute_turn_handler" in test_context:
            return await test_context["execute_turn_handler"](*args, **kwargs)
        return [], "default mock response"
    
    with patch("agent_base.execute_agent_turn", side_effect=dynamic_mock) as mock:
        yield mock

@pytest.fixture
def patch_subprocess(test_context):
    original_create = asyncio.create_subprocess_shell
    
    async def mock_create(*args, **kwargs):
        proc = await original_create(*args, **kwargs)
        test_context["bash_proc"] = proc
        test_context["bash_pgid"] = os.getpgid(proc.pid)
        return proc
        
    with patch("asyncio.create_subprocess_shell", side_effect=mock_create) as mock:
        yield mock

# Scenario Declarations
@pytest.mark.anyio
@scenario('features/concurrency_intervention.feature', 'POST requests return 409 Conflict when the agent is busy')
async def test_post_requests_conflict(mock_agent_turn):
    pass

@pytest.mark.anyio
@scenario('features/concurrency_intervention.feature', 'GET health queries run concurrently and are not blocked by active turns')
async def test_get_health_concurrent(mock_agent_turn):
    pass

@pytest.mark.anyio
@scenario('features/concurrency_intervention.feature', 'PUT requests cancel the active turn and terminate all subprocesses')
async def test_put_requests_cancel_turn(mock_agent_turn, patch_subprocess):
    pass

# Step Definitions
@given(parsers.parse('an agent application "{agent_name}" is running'))
async def agent_running(agent_name, test_context):
    import shutil
    try:
        shutil.rmtree("/tmp/documentary-pipeline")
    except Exception:
        pass
    os.makedirs("/tmp/documentary-pipeline", exist_ok=True)
    
    from agent_base import event_store
    event_store._init_db()
    
    app = make_agent_app(agent_name)
    test_context["app"] = app
    test_context["agent_name"] = agent_name

@when(parsers.parse('a long-running turn is triggered on "{agent_name}" via POST'))
async def trigger_long_running_post(agent_name, test_context):
    async def handle_long_turn(*args, **kwargs):
        test_context["turn_entered_event"].set()
        await test_context["turn_finish_event"].wait()
        return [], "first turn finished"
        
    test_context["execute_turn_handler"] = handle_long_turn
    
    async def run_first_post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_context["app"]), base_url="http://test") as client:
            resp = await client.post("/", content="First Prompt")
            test_context["first_post_response"] = resp
            
    test_context["first_post_task"] = asyncio.create_task(run_first_post())
    await test_context["turn_entered_event"].wait()
    await asyncio.sleep(0.1)

@when(parsers.parse('a concurrent POST request is sent to "{agent_name}"'))
async def concurrent_post_request(agent_name, test_context):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_context["app"]), base_url="http://test") as client:
        resp = await client.post("/", content="Second Prompt")
        test_context["second_post_response"] = resp

@then(parsers.parse('the second POST request must receive a 409 Conflict response'))
async def verify_409_conflict(test_context):
    assert test_context["second_post_response"].status_code == 409
    assert test_context["second_post_response"].text == "Agent is busy"

@then(parsers.parse('the first POST request should eventually complete successfully'))
async def verify_first_post_completes(test_context):
    test_context["turn_finish_event"].set()
    await test_context["first_post_task"]
    assert test_context["first_post_response"].status_code == 200
    assert test_context["first_post_response"].text == "first turn finished"

@when(parsers.parse('a GET health query is sent to "{agent_name}"'))
async def send_health_query(agent_name, test_context):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_context["app"]), base_url="http://test") as client:
        # Pass accept header to get JSON, and verify that the response status is busy
        resp = await client.get("/", headers={"accept": "application/json"})
        test_context["health_response"] = resp

@then(parsers.parse('the GET health query must return immediately with status 200'))
async def verify_health_response(test_context):
    assert test_context["health_response"].status_code == 200
    data = test_context["health_response"].json()
    assert data["status"] == "busy"

@when(parsers.parse('a turn running a long bash subprocess is triggered on "{agent_name}" via POST'))
async def trigger_bash_post(agent_name, test_context):
    test_context["call_count"] = 0
    
    async def handle_bash_turn(*args, **kwargs):
        test_context["call_count"] += 1
        if test_context["call_count"] == 1:
            test_context["turn_entered_event"].set()
            try:
                res = await bash_command(None, "sleep 100")
                return [], res
            except asyncio.CancelledError:
                test_context["cancelled_event"].set()
                raise
        else:
            return [], "new turn started"
            
    test_context["execute_turn_handler"] = handle_bash_turn
    
    async def run_bash_post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_context["app"]), base_url="http://test") as client:
            resp = await client.post("/", content="Trigger bash")
            test_context["first_post_response"] = resp
            
    test_context["first_post_task"] = asyncio.create_task(run_bash_post())
    await test_context["turn_entered_event"].wait()
    await asyncio.sleep(0.5)

@when(parsers.parse('a PUT request is sent to "{agent_name}"'))
async def send_put_request(agent_name, test_context):
    async def run_put():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_context["app"]), base_url="http://test") as client:
            resp = await client.put("/", content="Intervention PUT Prompt")
            test_context["put_response"] = resp
            
    test_context["put_task"] = asyncio.create_task(run_put())
    await test_context["put_task"]

@then(parsers.parse('the active turn must be cancelled immediately'))
async def verify_active_turn_cancelled(test_context):
    assert test_context["cancelled_event"].is_set()
    await test_context["first_post_task"]
    assert test_context["first_post_response"].status_code == 499

@then(parsers.parse('the running bash subprocess group must be terminated instantly'))
async def verify_subprocess_terminated(test_context):
    await asyncio.sleep(0.5)
    assert test_context["bash_proc"] is not None
    assert test_context["bash_proc"].returncode is not None

@then(parsers.parse('no orphan processes from that subprocess group must remain on the system'))
async def verify_no_orphans(test_context):
    pgid = test_context["bash_pgid"]
    assert pgid is not None
    try:
        os.killpg(pgid, 0)
        assert False, "Process group still exists, but it should have been killed"
    except ProcessLookupError:
        pass

@then(parsers.parse('a new turn must start on "{agent_name}"'))
async def verify_new_turn_starts(agent_name, test_context):
    assert test_context["put_response"].status_code == 204
    assert test_context["put_response"].text == ""
    
    # Wait for the background task triggered by PUT to complete
    from agent_base import active_tasks
    task = active_tasks.get(agent_name)
    if task:
        await task
    
    assert test_context["call_count"] == 2
