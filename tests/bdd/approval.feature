Feature: Human review of triage actions
  Scenario: A denied run finishes without recording a business action
    Given a run is waiting for human approval
    When the reviewer denies the action
    Then the run is rejected without a business action
