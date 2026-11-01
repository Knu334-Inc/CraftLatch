# 部分設定。バケット名にはアカウント ID が含まれるため、bucket と key は init 時に渡す（公開リポジトリに書かない）。
# terraform init -backend-config="bucket=<バケット名>" -backend-config="key=bootstrap/terraform.tfstate"
terraform {
  backend "s3" {
    region       = "ap-northeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
