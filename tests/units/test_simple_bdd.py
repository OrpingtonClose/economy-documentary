from pytest_bdd import scenarios, given, when, then

scenarios('features/scenario_agent.feature')

@given("the GSA event store is clean")
def clean():
    pass

@given("the Scenario Agent is running in the VM")
def running():
    pass

@when('the Scenario Agent receives an instruction to "Write a short documentary about Lacan\'s objet petit a."')
def receive():
    pass

@then('the GSA event store should contain an "update_script" effect')
def check_effect():
    pass

@then("the new script block text must be semantically coherent with the original topic")
def check_coherence():
    pass

@then('the semantic evaluation metric "script_coherence" must score above 0.85')
def check_metric():
    pass
