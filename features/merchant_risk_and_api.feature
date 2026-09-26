Feature: Merchant risk history and the operations API
  As an operations analyst
  I want risk applied as at the transaction date and KPIs exposed through an API
  So that the dashboard shows accurate, point-in-time information

  Scenario: Risk level is applied as at the transaction date
    Given merchant "M100" has risk "LOW" from "2026-01-01" to "2026-03-31"
    And merchant "M100" has risk "HIGH" from "2026-04-01" to "2026-06-30"
    And merchant "M100" has risk "MEDIUM" from "2026-07-01" onwards
    When a transaction for "M100" on "2026-02-10" is processed
    Then that transaction should be classified under risk level "LOW"
    And a transaction for "M100" on "2026-09-01" should be classified under risk level "MEDIUM"

  Scenario: Merchant risk windows never overlap in the dimension
    When the merchant dimension is built
    Then no merchant should have two validity windows covering the same date
    And each merchant should have exactly one current row

  Scenario: Settlement summary returns KPIs for a valid window
    Given the gold layer contains settlement data for "2026-09-01" to "2026-09-07"
    When a client requests the settlement summary for that window with a valid API key
    Then the response status should be 200
    And the response should contain transaction_count, transaction_amount, settled_amount, settlement_rate, settlement_gap and sla_rate
    And the settlement rate should be between 0 and 100

  Scenario: An invalid date is rejected
    When a client requests the settlement summary with start_date "01-09-2026"
    Then the response status should be 400
    And the error message should not disclose any database detail

  Scenario: An unknown merchant is reported as not found
    When a client requests the settlement summary for merchant "M4242" which does not exist
    Then the response status should be 404

  Scenario: An invalid parameter is rejected by schema validation
    When a client requests merchant exceptions with limit 0
    Then the response status should be 422

  Scenario: An unauthenticated request is rejected
    When a client requests the settlement summary without an API key
    Then the response status should be 401

  Scenario: Merchant exceptions list merchants breaching operational thresholds
    Given merchant "M1008" has a settlement rate of 88.4 percent and an SLA rate of 72.5 percent
    When a client requests the merchant exception report
    Then merchant "M1008" should appear in the response
    And the response should include its risk level and settlement gap
    And merchants meeting both thresholds should not appear
