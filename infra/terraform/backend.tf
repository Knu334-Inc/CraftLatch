# 部分設定。バケット名にはアカウント ID が含まれるため、bucket は init 時に渡す（公開リポジトリに書かない）。
# terraform init -backend-config="bucket=<バケット名>"
terraform {
  backend "s3" {
    key          = "craftlatch/terraform.tfstate"
    region       = "ap-northeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
