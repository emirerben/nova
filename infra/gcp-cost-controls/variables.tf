variable "monitoring_project_id" {
  description = "FinOps project that owns the Pub/Sub topic and email channel."
  type        = string
}

variable "alert_email" {
  description = "Monitored mailbox for every budget threshold."
  type        = string
}

variable "billing_accounts" {
  description = "Every billing account in the £30 combined target. Account amounts are derived from project_budgets."
  type = map(object({
    display_name = string
  }))
}

variable "project_budgets" {
  description = "Project budgets. The four allocations must sum to £30."
  type = map(object({
    display_name       = string
    billing_account_id = string
    project_number     = string
    monthly_budget_gbp = number
  }))
}

variable "export_datasets" {
  description = "One Standard Usage Cost export dataset per billing account."
  type = map(object({
    billing_account_id = string
    project_id         = string
    dataset_id         = string
    location           = optional(string, "EU")
  }))
}
