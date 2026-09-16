terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  backend "s3" {
    bucket       = "may26-hosp-reliability-tfstate"
    key          = "proxy/terraform.tfstate"
    region       = "eu-west-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "eu-west-2"
}

# ==========================================
# 1. ZIP PACKAGING FOR LAMBDA CODE
# ==========================================

data "archive_file" "proxy_payload" {
  type        = "zip"
  source_dir  = "${path.module}/lambda_src"
  output_path = "${path.module}/lambda_payload.zip"
  excludes    = ["__pycache__", "__pycache__/*"]
}

# ==========================================
# 2. DYNAMODB CACHE TABLE
# ==========================================

resource "aws_dynamodb_table" "cache" {
  name         = "hosp_api_cache"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "cache_key"

  attribute {
    name = "cache_key"
    type = "S"
  }
  attribute {
    name = "resource_path"
    type = "S"
  }

  global_secondary_index {
    name            = "ResourceIndex"
    hash_key        = "resource_path"
    projection_type = "KEYS_ONLY"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Name = "hosp-api-cache"
  }
}

# ==========================================
# 3. IAM ROLE & POLICIES FOR LAMBDA
# ==========================================

resource "aws_iam_role" "lambda_exec" {
  name = "hosp_proxy_lambda_execution_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_policy" "lambda_dynamodb_cache" {
  name        = "hosp_proxy_dynamodb_cache_policy"
  description = "Allows proxy Lambda to read, write, and invalidate DynamoDB cache items"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
          "dynamodb:CreateTable",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.cache.arn,
          "${aws_dynamodb_table.cache.arn}/index/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "attach_cache_policy" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_dynamodb_cache.arn
}

# Grants permissions to create ENIs inside the VPC
resource "aws_iam_role_policy_attachment" "lambda_vpc_access" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# ==========================================
# 4. VPC NETWORKING & SECURITY GROUPS
# ==========================================

resource "aws_security_group" "lambda_sg" {
  name        = "may26-lambda-proxy-sg"
  description = "Security group for Lambda proxy shield"
  vpc_id      = "vpc-080dbb0b7dc86503a"

  # Outbound to HOSP, the VPC DNS resolver, and AWS service endpoints
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "may26-lambda-proxy-sg"
  }
}

# VPC Gateway Endpoint for DynamoDB access inside private subnets
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = "vpc-080dbb0b7dc86503a"
  service_name      = "com.amazonaws.eu-west-2.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = ["rtb-0020e3ad9b254dde8"]

  tags = {
    Name = "dynamodb-vpc-endpoint"
  }
}

# Interface endpoint so the VPC Lambda can ship logs to CloudWatch
resource "aws_vpc_endpoint" "logs" {
  vpc_id              = "vpc-080dbb0b7dc86503a"
  service_name        = "com.amazonaws.eu-west-2.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = ["subnet-09f2ffa366a8abe67", "subnet-0fc0a94296b831a31"]
  security_group_ids  = [aws_security_group.lambda_sg.id]
  private_dns_enabled = true

  tags = {
    Name = "logs-vpc-endpoint"
  }
}

# ==========================================
# 5. ALB TARGET GROUP & CANARY ROUTING
# ==========================================

data "aws_lb_listener" "http_redirect_rule" {
  load_balancer_arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:loadbalancer/app/lb-may26/3cf64897dfb55cc8"
  port              = 80
}

data "aws_lb_listener" "https_default" {
  load_balancer_arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:loadbalancer/app/lb-may26/3cf64897dfb55cc8"
  port              = 443
}

data "aws_lb_target_group" "hosp" {
  arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:targetgroup/lb-tg-may26/d7eac9179951f0ca"
}

resource "aws_lb_target_group" "lambda_proxy" {
  name        = "may26-proxy-tg"
  target_type = "lambda"
}

resource "aws_lambda_permission" "alb" {
  statement_id  = "AllowALBInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.proxy_shield.function_name
  principal     = "elasticloadbalancing.amazonaws.com"
  source_arn    = aws_lb_target_group.lambda_proxy.arn
}

resource "aws_lb_target_group_attachment" "lambda_proxy" {
  target_group_arn = aws_lb_target_group.lambda_proxy.arn
  target_id        = aws_lambda_function.proxy_shield.arn
  depends_on       = [aws_lambda_permission.alb]
}

# variable "proxy_weight" {
#   type        = number
#   default     = 100 # Safe default: 0% traffic to Lambda proxy
#   description = "Percentage of traffic to send to the Lambda proxy (0-100)"
# }

# ==========================================
# 6. LAMBDA FUNCTION & ENVIRONMENT
# ==========================================

resource "aws_lambda_function" "proxy_shield" {
  filename         = data.archive_file.proxy_payload.output_path
  source_code_hash = data.archive_file.proxy_payload.output_base64sha256
  function_name    = "hosp_proxy_shield"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 25 # Accommodates slow HOSP calls + retries

  # Protect Puma from thread exhaustion by capping concurrency
  reserved_concurrent_executions = 8

  vpc_config {
    subnet_ids = [
      "subnet-09f2ffa366a8abe67",
      "subnet-0fc0a94296b831a31"
    ]
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  environment {
    variables = {
      CACHE_TABLE_NAME    = aws_dynamodb_table.cache.name
      HOSP_BACKEND_URL    = "http://172.31.39.164"
      CACHE_TTL_SECONDS   = "300" # Enforced as string
      RETRY_WRITES_ON_5XX = "true"
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_vpc_access
  ]
}

# ==========================================
# 7. OUTPUTS
# ==========================================

output "lambda_security_group_id" {
  value       = aws_security_group.lambda_sg.id
  description = "Provide this SG ID to coach to add HTTP :80 ingress rule on HOSP SG"
}

output "lambda_target_group_arn" {
  value       = aws_lb_target_group.lambda_proxy.arn
  description = "ARN for the Lambda target group"
}