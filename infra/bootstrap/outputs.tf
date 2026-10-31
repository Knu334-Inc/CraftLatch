# 管理者が GitHub の Secret を設定するときに参照する。CI はこれらの出力を読まない。
output "plan_role_arn" {
  description = "craftlatch-gha-plan の ARN（Secret AWS_PLAN_ROLE_ARN に設定する）"
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "craftlatch-gha-apply の ARN（Secret AWS_APPLY_ROLE_ARN に設定する）"
  value       = aws_iam_role.apply.arn
}

output "boundary_policy_arn" {
  description = "Permissions Boundary craftlatch-boundary の ARN"
  value       = aws_iam_policy.boundary.arn
}
