provider "google" {
  project = var.monitoring_project_id
}

locals {
  thresholds = flatten([
    for percent in [0.50, 0.75, 0.90, 1.00] : [
      { percent = percent, basis = "CURRENT_SPEND" },
      { percent = percent, basis = "FORECASTED_SPEND" },
    ]
  ])
  project_budget_total_gbp = sum([for budget in values(var.project_budgets) : budget.monthly_budget_gbp])
  account_budget_gbp = {
    for account_id, account in var.billing_accounts : account_id => sum([
      for budget in values(var.project_budgets) : budget.monthly_budget_gbp
      if budget.billing_account_id == account_id
    ])
  }
}

check "combined_budget_is_thirty_gbp" {
  assert {
    condition = (
      local.project_budget_total_gbp == 30 &&
      setequals(toset(keys(var.project_budgets)), toset(["production", "development", "omni", "storage"])) &&
      try(var.project_budgets.production.monthly_budget_gbp, -1) == 22 &&
      try(var.project_budgets.development.monthly_budget_gbp, -1) == 2 &&
      try(var.project_budgets.omni.monthly_budget_gbp, -1) == 2 &&
      try(var.project_budgets.storage.monthly_budget_gbp, -1) == 4 &&
      length(distinct([for budget in values(var.project_budgets) : budget.project_number])) == 4 &&
      length(var.billing_accounts) == 2 &&
      alltrue([
        for budget in values(var.project_budgets) : contains(keys(var.billing_accounts), budget.billing_account_id)
      ]) &&
      alltrue([for amount in values(local.account_budget_gbp) : amount > 0]) &&
      length(var.export_datasets) == length(var.billing_accounts) &&
      setequals(
        toset([for dataset in values(var.export_datasets) : dataset.billing_account_id]),
        toset(keys(var.billing_accounts))
      )
    )
    error_message = "Budgets must be production=22, development=2, omni=2, storage=4 across four distinct project numbers; exactly two funded billing accounts and one Standard export dataset per account are required."
  }
}

resource "google_project_service" "monitoring_apis" {
  for_each = toset([
    "billingbudgets.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
  ])

  project            = var.monitoring_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service" "bigquery" {
  for_each = var.export_datasets

  project            = each.value.project_id
  service            = "bigquery.googleapis.com"
  disable_on_destroy = false
}

resource "google_pubsub_topic" "cost_alerts" {
  project = var.monitoring_project_id
  name    = "nova-cost-alerts"

  depends_on = [google_project_service.monitoring_apis]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_pubsub_topic_iam_member" "billing_publisher" {
  project = var.monitoring_project_id
  topic   = google_pubsub_topic.cost_alerts.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:billing-budget-notifications@system.gserviceaccount.com"
}

resource "google_monitoring_notification_channel" "cost_email" {
  project      = var.monitoring_project_id
  display_name = "Nova cost controls — monitored email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.monitoring_apis]
}

resource "google_bigquery_dataset" "billing_export" {
  for_each = var.export_datasets

  project                    = each.value.project_id
  dataset_id                 = each.value.dataset_id
  friendly_name              = "Nova Standard Usage Cost export — ${each.key}"
  description                = "Cloud Billing Standard Usage Cost export; no default table expiry."
  location                   = each.value.location
  delete_contents_on_destroy = false

  depends_on = [google_project_service.bigquery]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_billing_budget" "account" {
  for_each = var.billing_accounts

  billing_account = each.key
  display_name    = "Nova account — ${each.value.display_name} — £${local.account_budget_gbp[each.key]}/month"

  amount {
    specified_amount {
      currency_code = "GBP"
      units         = tostring(local.account_budget_gbp[each.key])
    }
  }

  dynamic "threshold_rules" {
    for_each = local.thresholds
    content {
      threshold_percent = threshold_rules.value.percent
      spend_basis       = threshold_rules.value.basis
    }
  }

  all_updates_rule {
    pubsub_topic                   = google_pubsub_topic.cost_alerts.id
    schema_version                 = "1.0"
    monitoring_notification_channels = [google_monitoring_notification_channel.cost_email.id]
    disable_default_iam_recipients = false
  }

  depends_on = [google_pubsub_topic_iam_member.billing_publisher]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_billing_budget" "project" {
  for_each = var.project_budgets

  billing_account = each.value.billing_account_id
  display_name    = "Nova project — ${each.value.display_name} — £${each.value.monthly_budget_gbp}/month"

  budget_filter {
    projects               = ["projects/${each.value.project_number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "GBP"
      units         = tostring(each.value.monthly_budget_gbp)
    }
  }

  dynamic "threshold_rules" {
    for_each = local.thresholds
    content {
      threshold_percent = threshold_rules.value.percent
      spend_basis       = threshold_rules.value.basis
    }
  }

  all_updates_rule {
    pubsub_topic                      = google_pubsub_topic.cost_alerts.id
    schema_version                    = "1.0"
    monitoring_notification_channels = [google_monitoring_notification_channel.cost_email.id]
    disable_default_iam_recipients    = false
    enable_project_level_recipients   = true
  }

  depends_on = [google_pubsub_topic_iam_member.billing_publisher]

  lifecycle {
    prevent_destroy = true
  }
}

output "budget_pubsub_topic" {
  value = google_pubsub_topic.cost_alerts.id
}

output "standard_export_datasets" {
  value = {
    for key, dataset in google_bigquery_dataset.billing_export : key => {
      billing_account_id = var.export_datasets[key].billing_account_id
      project_id         = dataset.project
      dataset_id         = dataset.dataset_id
      location           = dataset.location
    }
  }
}
