Feature: Settlement reconciliation for merchant payments
  As the Head of Payments
  I want every successful payment reconciled against its settlements
  So that I can explain the difference between what customers paid and what merchants received

  Background:
    Given the merchant master contains merchant "M100" with risk level "MEDIUM" effective from "2026-07-01"
    And the settlement SLA is 30 minutes

  Scenario: Identify an unsettled successful transaction
    Given a successful payment "T5001" of 3000.00 INR exists for merchant "M100" on "2026-09-01"
    And no settlement record exists for "T5001"
    When the settlement pipeline runs
    Then transaction "T5001" should be classified as "UNSETTLED"
    And it should appear in the settlement exception report
    And the settlement gap for merchant "M100" on "2026-09-01" should include 3000.00

  Scenario: Aggregate a one-to-many settlement before transaction level maths
    Given a successful payment "T1001" of 10000.00 INR exists
    And settlement "S5001" of 8000.00 with status "SETTLED" exists for "T1001"
    And settlement "S5002" of 2000.00 with status "SETTLED" exists for "T1001"
    When the settlement pipeline runs
    Then the reconciled settled amount for "T1001" should be 10000.00
    And the transaction amount for "T1001" should be counted exactly once
    And transaction "T1001" should be classified as "FULLY_SETTLED"

  Scenario: Partial settlement leaves a residual gap
    Given a successful payment "T1002" of 8000.00 INR exists
    And settlement "S5003" of 5000.00 with status "SETTLED" exists for "T1002"
    When the settlement pipeline runs
    Then transaction "T1002" should be classified as "PARTIALLY_SETTLED"
    And the settlement gap for "T1002" should be 3000.00

  Scenario: Pending settlements are not counted as settled money
    Given a successful payment "T1003" of 6000.00 INR exists
    And settlement "S5004" of 6000.00 with status "PENDING" exists for "T1003"
    When the settlement pipeline runs
    Then the settled amount for "T1003" should be 0.00
    And transaction "T1003" should be classified as "PENDING"
    And the settlement rate should not include "T1003" as settled value

  Scenario: Settlement SLA is measured to the first settled record
    Given a successful payment "T1004" at "2026-09-01 10:00:00" of 5000.00 INR exists
    And settlement "S5005" with status "SETTLED" occurs at "2026-09-01 10:05:00"
    When the settlement pipeline runs
    Then the minutes to settle for "T1004" should be 5
    And transaction "T1004" should be marked as meeting the settlement SLA

  Scenario: Settlement beyond the SLA window is reported as a breach
    Given a successful payment "T1005" at "2026-09-01 11:00:00" of 5000.00 INR exists
    And settlement "S5006" with status "SETTLED" occurs at "2026-09-01 12:00:00"
    When the settlement pipeline runs
    Then the minutes to settle for "T1005" should be 60
    And transaction "T1005" should be marked as breaching the settlement SLA

  Scenario: Failed and reversed payments are excluded from settlement KPIs
    Given a payment "T1006" of 2000.00 INR with status "FAILED" exists
    And a payment "T1007" of 1500.00 INR with status "REVERSED" exists
    When the settlement pipeline runs
    Then neither "T1006" nor "T1007" should appear in the settlement reconciliation
    And the transaction volume KPI should exclude 3500.00
