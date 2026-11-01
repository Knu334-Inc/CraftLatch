# CI から入力する変数は無い（すべて既定値を使う）。
variable "region" {
  description = "AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "リソース名の接頭辞。bootstrap の apply ロールの権限は craftlatch- の名前に限定しているため、既定値から変えない"
  type        = string
  default     = "craftlatch"
}
