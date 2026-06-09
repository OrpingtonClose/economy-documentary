#!/usr/bin/env python3
import sys
import argparse
import os
import ast

def check_file(filepath):
    # Normalize path
    abs_path = os.path.abspath(filepath)
    filename = os.path.basename(abs_path)
    
    # Only enforce checks on files inside server/capabilities/ directory
    if "server/capabilities" not in abs_path or not filename.endswith(".py"):
        return True

    # Read the file content
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"❌ SC Guard Enforcer: Could not read file {abs_path}: {e}")
        return False

    # Parse AST
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        print(f"❌ SC Guard Enforcer: Syntax error in file {abs_path}: {e}")
        return False

    errors = []

    # AST Visitor to scan for mock/skip cheat violations
    class SCVisitor(ast.NodeVisitor):
        def visit_Import(self, node):
            for alias in node.names:
                if any(x in alias.name for x in ["mock", "unittest.mock", "pytest_mock"]):
                    errors.append(f"Forbidden mock import: 'import {alias.name}' at line {node.lineno}")
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if node.module and any(x in node.module for x in ["mock", "unittest.mock", "pytest_mock"]):
                errors.append(f"Forbidden mock import: 'from {node.module} import ...' at line {node.lineno}")
            for alias in node.names:
                if any(x in alias.name for x in ["mock", "MagicMock", "Mock", "PropertyMock"]):
                    errors.append(f"Forbidden mock import: '{alias.name}' from '{node.module}' at line {node.lineno}")
            self.generic_visit(node)

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name):
                if node.value.id in ["mock", "unittest", "pytest"]:
                    if any(x in node.attr for x in ["patch", "MagicMock", "Mock", "PropertyMock", "skip"]):
                        errors.append(f"Forbidden mock/skip usage: '{node.value.id}.{node.attr}' at line {node.lineno}")
            self.generic_visit(node)

        def visit_Name(self, node):
            if node.id in ["MagicMock", "Mock", "PropertyMock"]:
                errors.append(f"Forbidden mock usage: '{node.id}' at line {node.lineno}")
            self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest" and node.func.attr == "skip":
                    errors.append(f"Forbidden skip call: 'pytest.skip(...)' at line {node.lineno}")
            elif isinstance(node.func, ast.Name) and node.func.id == "skip":
                errors.append(f"Forbidden skip call: 'skip(...)' at line {node.lineno}")
            self.generic_visit(node)

        def visit_Assert(self, node):
            test = node.test
            is_trivial = False
            
            # assert True / assert False / assert Constant
            if isinstance(test, ast.Constant):
                is_trivial = True
            
            # assert Constant == Constant
            elif isinstance(test, ast.Compare):
                if len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                    left = test.left
                    right = test.comparators[0]
                    if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
                        is_trivial = True
            
            if is_trivial:
                errors.append(f"Forbidden trivial assertion at line {node.lineno}")
                
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            for dec in node.decorator_list:
                dec_str = ast.dump(dec)
                if "skip" in dec_str.lower():
                    errors.append(f"Forbidden skip decorator on function '{node.name}' at line {node.lineno}")
            self.generic_visit(node)

        def visit_ClassDef(self, node):
            for dec in node.decorator_list:
                dec_str = ast.dump(dec)
                if "skip" in dec_str.lower():
                    errors.append(f"Forbidden skip decorator on class '{node.name}' at line {node.lineno}")
            self.generic_visit(node)

    visitor = SCVisitor()
    visitor.visit(tree)

    if errors:
        print(f"❌ SC Guard Enforcer: Rejected Simulation Cover file edit: {filename}")
        for err in errors:
            print(f"  - {err}")
        return False

    return True

def main():
    parser = argparse.ArgumentParser(description="Simulation Cover Guard Enforcer Plugin")
    parser.add_argument("--file", required=True, help="TargetFile to audit")
    args = parser.parse_args()

    success = check_file(args.file)
    if not success:
        sys.exit(1)
    
    print(f"✅ SC Guard Enforcer: File '{os.path.basename(args.file)}' passed validation.")
    sys.exit(0)

if __name__ == "__main__":
    main()
