data "aws_caller_identity" "current" {}

locals {
  # アカウント ID は変数にせず実行時に取得する（公開リポジトリに書かない）。
  account_id = data.aws_caller_identity.current.account_id

  auth_role_arn    = "arn:aws:iam::${local.account_id}:role/craftlatch-auth-role"
  host_user_arn    = "arn:aws:iam::${local.account_id}:user/craftlatch-host"
  grants_queue_arn = "arn:aws:sqs:${var.region}:${local.account_id}:craftlatch-grants"

  state_bucket_arn = "arn:aws:s3:::${var.state_bucket}"
  state_object_arn = "arn:aws:s3:::${var.state_bucket}/craftlatch/terraform.tfstate"
  state_lock_arn   = "arn:aws:s3:::${var.state_bucket}/craftlatch/terraform.tfstate.tflock"
}

################################################################################
# 共通: 明示的な拒否（apply ロールの権限昇格対策。plan ロールにも多層防御として付ける）
################################################################################

data "aws_iam_policy_document" "protected_deny" {
  # Boundary の削除・付け替えを禁止する。
  statement {
    sid    = "DenyPermissionsBoundaryChanges"
    effect = "Deny"
    actions = [
      "iam:DeleteRolePermissionsBoundary",
      "iam:DeleteUserPermissionsBoundary",
      "iam:PutRolePermissionsBoundary",
      "iam:PutUserPermissionsBoundary",
    ]
    resources = ["*"]
  }

  # GHA 用ロール・Boundary（ポリシーバージョンを含む）・OIDC プロバイダーへの書き込みを禁止する。
  statement {
    sid    = "DenyWritesToBootstrapResources"
    effect = "Deny"
    actions = [
      "iam:Add*",
      "iam:Attach*",
      "iam:Create*",
      "iam:Delete*",
      "iam:Detach*",
      "iam:PassRole",
      "iam:Put*",
      "iam:Remove*",
      "iam:SetDefaultPolicyVersion",
      "iam:Tag*",
      "iam:Untag*",
      "iam:Update*",
    ]
    resources = [
      "arn:aws:iam::${local.account_id}:role/craftlatch-gha-*",
      aws_iam_policy.boundary.arn,
      local.github_oidc_provider_arn,
    ]
  }
}

################################################################################
# 共通: state バケットの権限（設計書 4.7「state バケットの権限」）
# craftlatch/ の 2 キーのみ。bootstrap/ 配下とバケット設定の変更は許可しない。
################################################################################

data "aws_iam_policy_document" "state_plan" {
  statement {
    sid       = "StateRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = [local.state_object_arn]
  }

  statement {
    sid    = "StateLock"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [local.state_lock_arn]
  }

  # S3 backend はワークスペース一覧を prefix env:/ で取得するが、既定の workspace_key_prefix では
  # AccessDenied を無視して default のみとして扱うため、env:/ は許可しない。
  statement {
    sid       = "StateList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["craftlatch/*"]
    }
  }
}

data "aws_iam_policy_document" "state_apply" {
  statement {
    sid    = "StateReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      local.state_object_arn,
      local.state_lock_arn,
    ]
  }

  statement {
    sid       = "StateList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["craftlatch/*"]
    }
  }
}

################################################################################
# plan ロール（PR での plan。読み取り専用）
################################################################################

# 共有アカウントのため、読み取りも CraftLatch のリソースに限定する（他のシステムの Lambda の環境変数・コード、
# IAM、キュー属性などを PR のコードから読めないようにする）。アクションは terraform plan の refresh と
# data source がプロバイダー（hashicorp/aws v6.71）から呼ぶ読み取り API に限る。SQS のメッセージ受信は含まない。
data "aws_iam_policy_document" "plan_read" {
  # aws_cognito_user_group・aws_cognito_user_pool_client・aws_cognito_managed_login_branding の読み取り。
  # 対象は User Pool の ARN で、User Pool のタグで判定される。
  statement {
    sid    = "PlanReadTaggedUserPools"
    effect = "Allow"
    actions = [
      "cognito-idp:DescribeManagedLoginBranding",
      "cognito-idp:DescribeManagedLoginBrandingByClient",
      "cognito-idp:DescribeUserPoolClient",
      "cognito-idp:GetGroup",
      "cognito-idp:ListUserPoolClients",
    ]
    resources = ["arn:aws:cognito-idp:${var.region}:${local.account_id}:userpool/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # ListUserPools（data "aws_cognito_user_pools" の名前検索）と DescribeUserPoolDomain（ドメイン名だけを受け取る）は
  # リソース種別を持たない（ARN で限定できない）ため * とする。User Pool の名前・ID とドメインの情報のみで、ユーザーは読めない。
  statement {
    sid    = "PlanCognitoUnscopedReads"
    effect = "Allow"
    actions = [
      "cognito-idp:DescribeUserPoolDomain",
      "cognito-idp:ListUserPools",
    ]
    resources = ["*"]
  }

  # HTTP API とその配下（統合・ルート・ステージ）の GET。配下のリソースは API のタグで判定される。
  statement {
    sid       = "PlanReadTaggedApis"
    effect    = "Allow"
    actions   = ["apigateway:GET"]
    resources = ["arn:aws:apigateway:${var.region}::/apis/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # タグの読み取り（GET /tags/<ARN>）。/tags は aws:ResourceTag に対応しないため名前・タグで限定できない。タグの読み取りのみ。
  statement {
    sid       = "PlanReadApiTags"
    effect    = "Allow"
    actions   = ["apigateway:GET"]
    resources = ["arn:aws:apigateway:${var.region}::/tags/*"]
  }

  # aws_lambda_function（GetFunction・ListVersionsByFunction・GetFunctionCodeSigningConfig、タグ）と
  # aws_lambda_permission（GetPolicy）の読み取り。
  statement {
    sid    = "PlanReadCraftlatchFunctions"
    effect = "Allow"
    actions = [
      "lambda:GetFunction",
      "lambda:GetFunctionCodeSigningConfig",
      "lambda:GetPolicy",
      "lambda:ListTags",
      "lambda:ListVersionsByFunction",
    ]
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:craftlatch-*"]
  }

  # aws_sqs_queue・aws_sqs_queue_policy の読み取り（属性とタグ）。
  statement {
    sid    = "PlanReadGrantsQueue"
    effect = "Allow"
    actions = [
      "sqs:GetQueueAttributes",
      "sqs:ListQueueTags",
    ]
    resources = [local.grants_queue_arn]
  }

  # aws_cloudwatch_log_group のタグの読み取り。
  statement {
    sid       = "PlanReadCraftlatchLogGroups"
    effect    = "Allow"
    actions   = ["logs:ListTagsForResource"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/craftlatch-*"]
  }

  # aws_cloudwatch_log_group の読み取りは DescribeLogGroups（名前の前方一致）で行う。
  # DescribeLogGroups はリソース種別を持たない（ARN で限定できない）ため * とする。ロググループの名前・設定の一覧のみで、ログ本文は読めない。
  statement {
    sid       = "PlanDescribeLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  # aws_iam_role・aws_iam_role_policy・aws_iam_user・aws_iam_user_policy の読み取り。
  statement {
    sid    = "PlanReadBoundedPrincipals"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:GetUser",
      "iam:GetUserPolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:ListUserTags",
    ]
    resources = [
      local.auth_role_arn,
      local.host_user_arn,
    ]
  }

  # data "aws_iam_policy" "boundary" の読み取り。GetPolicyVersion の対象はポリシーの ARN（バージョンを含む）。
  statement {
    sid    = "PlanReadBoundaryPolicy"
    effect = "Allow"
    actions = [
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyTags",
    ]
    resources = [aws_iam_policy.boundary.arn]
  }

  # name 指定の data "aws_iam_policy" は iam:ListPolicies で検索する。
  # ListPolicies はリソース単位の制限に対応しない（リソース種別なし）ため * とする。ポリシーの一覧のみで、内容は読めない。
  statement {
    sid       = "PlanListPoliciesForBoundaryLookup"
    effect    = "Allow"
    actions   = ["iam:ListPolicies"]
    resources = ["*"]
  }

  # PR のコードから利用者名・IP（ログの本文、User Pool のユーザー一覧）を読めないようにする。
  # plan には不要な操作で、上の許可には含まれないが、許可を広げたときの多層防御として拒否する。
  statement {
    sid    = "PlanDenyUserDataReads"
    effect = "Deny"
    actions = [
      "cognito-idp:ListUsers",
      "cognito-idp:ListUsersInGroup",
      "logs:GetLogEvents",
      "logs:GetLogRecord",
      "logs:GetQueryResults",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "plan" {
  source_policy_documents = [
    data.aws_iam_policy_document.plan_read.json,
    data.aws_iam_policy_document.state_plan.json,
    data.aws_iam_policy_document.protected_deny.json,
  ]
}

resource "aws_iam_role" "plan" {
  name               = "craftlatch-gha-plan"
  description        = "CraftLatch: GitHub Actions role for read-only PR plans"
  assume_role_policy = data.aws_iam_policy_document.plan_trust.json
}

resource "aws_iam_role_policy" "plan" {
  name   = "craftlatch-gha-plan"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}

################################################################################
# apply ロール（main での apply。Environment production）
################################################################################

# IAM: 権限昇格を防ぐため、作成・付与・受け渡しのステートメントを分ける（設計書 4.7 の表）。
data "aws_iam_policy_document" "apply_iam" {
  # 作成は 2 つの正確な ARN のみ、かつ Boundary の付与を必須にする。
  statement {
    sid    = "IamCreateBoundedPrincipals"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:CreateUser",
    ]
    resources = [
      local.auth_role_arn,
      local.host_user_arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.boundary.arn]
    }
  }

  # 付与・更新・削除はインラインポリシーのみ（iam:Attach* は許可しない）。読み取り系を含む。
  statement {
    sid    = "IamManageBoundedPrincipals"
    effect = "Allow"
    actions = [
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DeleteUser",
      "iam:DeleteUserPolicy",
      "iam:PutRolePolicy",
      "iam:PutUserPolicy",
      "iam:TagRole",
      "iam:TagUser",
      "iam:UpdateAssumeRolePolicy",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:GetUser",
      "iam:GetUserPolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListAttachedUserPolicies",
      "iam:ListGroupsForUser",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:ListUserPolicies",
      "iam:ListUserTags",
    ]
    resources = [
      local.auth_role_arn,
      local.host_user_arn,
    ]
  }

  statement {
    sid       = "IamPassAuthRoleToLambda"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [local.auth_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  # Task 11 の data "aws_iam_policy" "boundary"（name 指定）の読み取り。書き込みは protected_deny で拒否する。
  statement {
    sid    = "IamReadBoundaryPolicy"
    effect = "Allow"
    actions = [
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyTags",
    ]
    resources = [aws_iam_policy.boundary.arn]
  }

  # name 指定の data "aws_iam_policy" は iam:ListPolicies で検索する。
  # ListPolicies はリソース単位の制限に対応しない（Service Authorization Reference でリソース種別なし）ため * とする。読み取りのみ。
  statement {
    sid       = "IamListPoliciesForBoundaryLookup"
    effect    = "Allow"
    actions   = ["iam:ListPolicies"]
    resources = ["*"]
  }
}

# アプリケーションのリソース（Cognito・API Gateway・Lambda・SQS・CloudWatch Logs）。
# ARN に生成 ID を含むサービス（Cognito、API Gateway）はタグ Project=CraftLatch で、それ以外は名前で限定する。
data "aws_iam_policy_document" "apply_app" {
  # CreateUserPool はリソース種別を持たない（ARN で限定できない）ため * とし、作成時のタグで限定する。
  statement {
    sid       = "CognitoCreateTaggedUserPool"
    effect    = "Allow"
    actions   = ["cognito-idp:CreateUserPool"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # タグ付きで作成するときに必要な TagResource（CreateUserPool の依存アクション）。
  # 作成前のリソースには aws:ResourceTag がないため、付けるタグで限定する。
  # 残存リスク: 同じアカウントのタグのない User Pool には Project=CraftLatch を付けて管理下に入れられる。
  # アカウントを CraftLatch 専用で使う前提で受容する。他の値のタグが付いた User Pool は下の
  # CognitoDenyRetagOtherPools で拒否する。この文を削除できるかは U11（Task 14）で確認する。
  statement {
    sid       = "CognitoTagOnCreate"
    effect    = "Allow"
    actions   = ["cognito-idp:TagResource"]
    resources = ["arn:aws:cognito-idp:${var.region}:${local.account_id}:userpool/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # Project タグが CraftLatch 以外の値で付いている User Pool のタグ変更を拒否する（他の User Pool の乗っ取り防止）。
  statement {
    sid    = "CognitoDenyRetagOtherPools"
    effect = "Deny"
    actions = [
      "cognito-idp:TagResource",
      "cognito-idp:UntagResource",
    ]
    resources = ["*"]

    condition {
      test     = "Null"
      variable = "aws:ResourceTag/Project"
      values   = ["false"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # 既存の User Pool とその配下（アプリクライアント・ドメイン・グループ・ブランディング）。
  # これらのアクションの対象は User Pool の ARN で、User Pool のタグで判定される。
  # ListUserPoolClients は aws_cognito_managed_login_branding の読み取り（refresh のたび）で呼ばれる。
  statement {
    sid    = "CognitoManageTaggedUserPools"
    effect = "Allow"
    actions = [
      "cognito-idp:CreateGroup",
      "cognito-idp:CreateManagedLoginBranding",
      "cognito-idp:CreateUserPoolClient",
      "cognito-idp:CreateUserPoolDomain",
      "cognito-idp:DeleteGroup",
      "cognito-idp:DeleteManagedLoginBranding",
      "cognito-idp:DeleteUserPool",
      "cognito-idp:DeleteUserPoolClient",
      "cognito-idp:DeleteUserPoolDomain",
      "cognito-idp:DescribeManagedLoginBranding",
      "cognito-idp:DescribeManagedLoginBrandingByClient",
      "cognito-idp:DescribeUserPool",
      "cognito-idp:DescribeUserPoolClient",
      "cognito-idp:GetGroup",
      "cognito-idp:GetUserPoolMfaConfig",
      "cognito-idp:ListTagsForResource",
      "cognito-idp:ListUserPoolClients",
      "cognito-idp:SetUserPoolMfaConfig",
      "cognito-idp:TagResource",
      "cognito-idp:UntagResource",
      "cognito-idp:UpdateGroup",
      "cognito-idp:UpdateManagedLoginBranding",
      "cognito-idp:UpdateUserPool",
      "cognito-idp:UpdateUserPoolClient",
      "cognito-idp:UpdateUserPoolDomain",
    ]
    resources = ["arn:aws:cognito-idp:${var.region}:${local.account_id}:userpool/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # DescribeUserPoolDomain はドメイン名だけを受け取り、リソース種別を持たないため * とする。読み取りのみ。
  statement {
    sid       = "CognitoDescribeDomain"
    effect    = "Allow"
    actions   = ["cognito-idp:DescribeUserPoolDomain"]
    resources = ["*"]
  }

  # HTTP API の作成（POST /apis）は作成時のタグで限定する。
  statement {
    sid       = "ApiGatewayCreateTaggedApi"
    effect    = "Allow"
    actions   = ["apigateway:POST"]
    resources = ["arn:aws:apigateway:${var.region}::/apis"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # 既存の HTTP API とその配下（ルート・統合・ステージ）。配下のリソースは API のタグで判定される。
  # PUT（OpenAPI による再インポート）は使わないため許可しない。
  statement {
    sid    = "ApiGatewayManageTaggedApis"
    effect = "Allow"
    actions = [
      "apigateway:DELETE",
      "apigateway:GET",
      "apigateway:PATCH",
      "apigateway:POST",
    ]
    resources = ["arn:aws:apigateway:${var.region}::/apis/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["CraftLatch"]
    }
  }

  # タグの読み取り（GET /tags/<ARN>）。/tags は aws:ResourceTag に対応しないため名前・タグで限定できない。読み取りのみ。
  # タグの追加・削除（POST・DELETE /tags）は他の API に Project=CraftLatch を付けて管理下に入れられるため許可しない。
  statement {
    sid       = "ApiGatewayReadTags"
    effect    = "Allow"
    actions   = ["apigateway:GET"]
    resources = ["arn:aws:apigateway:${var.region}::/tags/*"]
  }

  statement {
    sid    = "LambdaManageCraftlatchFunctions"
    effect = "Allow"
    actions = [
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:GetFunction",
      "lambda:GetFunctionCodeSigningConfig",
      "lambda:GetFunctionConcurrency",
      "lambda:GetFunctionConfiguration",
      "lambda:GetPolicy",
      "lambda:ListTags",
      "lambda:ListVersionsByFunction",
      "lambda:RemovePermission",
      "lambda:TagResource",
      "lambda:UntagResource",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:craftlatch-*"]
  }

  # 呼び出し許可の追加は API Gateway からの呼び出しに限る（関数 URL・他サービスへの公開を防ぐ）。
  statement {
    sid       = "LambdaAddPermissionForApiGateway"
    effect    = "Allow"
    actions   = ["lambda:AddPermission"]
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:craftlatch-*"]

    condition {
      test     = "StringEquals"
      variable = "lambda:Principal"
      values   = ["apigateway.amazonaws.com"]
    }
  }

  # キューとキューポリシー（SetQueueAttributes の Policy 属性）。メッセージの送受信は許可しない。
  statement {
    sid    = "SqsManageGrantsQueue"
    effect = "Allow"
    actions = [
      "sqs:CreateQueue",
      "sqs:DeleteQueue",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ListQueueTags",
      "sqs:SetQueueAttributes",
      "sqs:TagQueue",
      "sqs:UntagQueue",
    ]
    resources = [local.grants_queue_arn]
  }

  statement {
    sid    = "LogsManageCraftlatchLogGroups"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:DeleteRetentionPolicy",
      "logs:ListTagsForResource",
      "logs:ListTagsLogGroup",
      "logs:PutRetentionPolicy",
      "logs:TagLogGroup",
      "logs:TagResource",
      "logs:UntagLogGroup",
      "logs:UntagResource",
    ]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/craftlatch-*"]
  }

  # DescribeLogGroups はリソース種別を持たない（ARN で限定できない）ため * とする。読み取りのみ。
  statement {
    sid       = "LogsDescribeLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "apply" {
  source_policy_documents = [
    data.aws_iam_policy_document.apply_iam.json,
    data.aws_iam_policy_document.apply_app.json,
    data.aws_iam_policy_document.state_apply.json,
    data.aws_iam_policy_document.protected_deny.json,
  ]
}

resource "aws_iam_role" "apply" {
  name               = "craftlatch-gha-apply"
  description        = "CraftLatch: GitHub Actions role for main apply (Environment production)"
  assume_role_policy = data.aws_iam_policy_document.apply_trust.json
}

resource "aws_iam_role_policy" "apply" {
  name   = "craftlatch-gha-apply"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.apply.json
}
