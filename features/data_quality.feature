Feature: Data quality handling in the settlement pipeline
  As a data engineer
  I want invalid records routed rather than dropped
  So that operations can see and fix every record the platform could not use

  Scenario: A settlement without a matching transaction is flagged as an orphan
    Given a settlement "S9001" references transaction "T9999"
    And no transaction "T9999" exists in the platform
    When the settlement pipeline runs
    Then settlement "S9001" should be loaded and flagged as an orphan
    And it should be recorded as a business exception under rule "STL-006"
    And it should not contribute to any merchant settlement rate

  Scenario: A transaction without a merchant identifier is quarantined
    Given a successful payment "T9002" of 900.00 INR has no merchant_id
    When the settlement pipeline runs
    Then transaction "T9002" should not be loaded into the silver layer
    And it should be written to the quarantine table under rule "TXN-005"
    And the quarantine record should retain the original source payload

  Scenario: A negative settlement amount is quarantined for operations review
    Given a settlement "S9003" for transaction "T9003" has an amount of -500.00
    When the settlement pipeline runs
    Then settlement "S9003" should not be loaded into the silver layer
    And it should be written to the quarantine table under rule "STL-004"
    And the settled amount for "T9003" should be unaffected by "S9003"

  Scenario: A duplicate event identifier is deduplicated on first arrival
    Given event "E9004" arrives at "10:00:05" and again at "10:09:00"
    When the settlement pipeline runs
    Then exactly one row for event "E9004" should exist in the silver layer
    And the retained row should have the ingestion timestamp "10:00:05"
    And rule "EVT-004" should report one deduplicated record

  Scenario: An unknown merchant identifier is loaded against the UNKNOWN dimension member
    Given a successful payment "T9005" references merchant "M9999" which is absent from the merchant master
    When the settlement pipeline runs
    Then transaction "T9005" should be loaded with merchant key -1
    And rule "TXN-010" should report a referential integrity warning
    And the transaction value should still reconcile to the daily total

  Scenario: A transaction in an unsupported currency is quarantined
    Given a successful payment "T9006" of 7000.00 is recorded in "USD"
    When the settlement pipeline runs
    Then transaction "T9006" should be quarantined under rule "TXN-006"
    And the INR settlement KPIs should be unaffected

  Scenario: Every source record is either loaded or recorded as an exception
    Given a batch of source transactions is ingested
    When the settlement pipeline runs
    Then the count of source rows should equal loaded rows plus quarantined rows plus deduplicated rows
