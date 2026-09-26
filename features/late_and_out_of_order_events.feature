Feature: Late arriving and out-of-order payment events
  As a data engineer
  I want business time separated from processing time
  So that KPIs stay correct when events arrive late or out of sequence

  Scenario: Business time drives the reporting day, not ingestion time
    Given event "E8001" has an event timestamp of "2026-09-01 23:58:00"
    And it has an ingestion timestamp of "2026-09-02 00:06:00"
    When the settlement pipeline runs
    Then the event should be reported against the date "2026-09-01"
    And it should be flagged as late arriving

  Scenario: Late arrival within the SLA is a warning, not an error
    Given event "E8002" arrives 511 seconds after it occurred
    When the settlement pipeline runs
    Then the event should be flagged as late arriving under rule "EVT-005"
    And it should not be flagged as an ingestion SLA breach

  Scenario: Late arrival beyond the SLA is a business exception
    Given event "E8003" arrives 1800 seconds after it occurred
    When the settlement pipeline runs
    Then the event should be flagged as an ingestion SLA breach under rule "EVT-006"
    And it should appear in the data quality report

  Scenario: Out-of-order arrival does not change the business event sequence
    Given event "AUTHORIZED" for "T8004" arrives before event "CREATED" for "T8004"
    When the settlement pipeline runs
    Then ordering the lifecycle by event timestamp should give "CREATED" then "AUTHORIZED"
    And ordering by ingestion timestamp should give "AUTHORIZED" then "CREATED"

  Scenario: A settlement arriving after the reporting day is closed reopens that day
    Given transaction "T8005" of 3000.00 on "2026-09-01" was previously classified as "UNSETTLED"
    And a settlement of 3000.00 for "T8005" arrives on "2026-09-03"
    When the incremental pipeline runs
    Then transaction "T8005" should be reclassified as "FULLY_SETTLED"
    And only the affected date and merchant partition should be recomputed
    And the settled amount reported for "2026-09-01" should increase by 3000.00
