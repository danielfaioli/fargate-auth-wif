# ECS Fargate → Microsoft Entra ID → Azure Databricks: secretless authentication

This guide describes how to let a workload running on **AWS ECS Fargate** query **Azure Databricks**
(SQL Warehouse) **without storing any secret** — no client secrets, certificates, or Databricks
personal access tokens. The workload proves its identity with a short-lived token issued by AWS, and
Microsoft Entra ID trusts that token through **workload identity federation** configured on a
**user-assigned managed identity**.

The approach was validated end to end in a POC environment (see [Validation](#7-validation)). This
document lists everything that must be configured in AWS, Azure, and Databricks for a production
environment where the ECR repository and ECS cluster already exist.

---

## 1. How it works

```
┌──────────────────────────── AWS account ────────────────────────────┐
│                                                                     │
│  ECS Fargate task  ──(runs as)──►  IAM task role                    │
│        │                                                            │
│        │ ① sts:GetWebIdentityToken  (audience: api://AzureADTokenExchange)
│        ▼                                                            │
│  AWS STS returns a signed JWT                                       │
│     iss = https://<id>.tokens.sts.global.api.aws   (account issuer) │
│     sub = arn:aws:iam::<account>:role/<task role>                   │
└────────┬────────────────────────────────────────────────────────────┘
         │ ② JWT presented as a client assertion
         ▼
┌──────────────────────────── Azure tenant ───────────────────────────┐
│  Microsoft Entra ID                                                 │
│   • fetches the AWS issuer's public signing keys and validates JWT  │
│   • matches iss / sub / aud against the federated credential        │
│     on the user-assigned managed identity                           │
│   • issues an access token for the Azure Databricks resource        │
│        │                                                            │
│        │ ③ Bearer token                                             │
│        ▼                                                            │
│  Azure Databricks workspace                                         │
│   • request runs as the managed identity's service principal        │
│   • SQL Statement Execution API → SQL Warehouse → Unity Catalog     │
└─────────────────────────────────────────────────────────────────────┘
```

1. **AWS → AWS STS.** The task uses its IAM task role credentials (provided automatically by ECS) to
   call `sts:GetWebIdentityToken`. This requires **IAM outbound identity federation** to be enabled on
   the AWS account. STS returns a JWT (valid 5 minutes) signed by keys published at an
   account-specific public issuer URL.
2. **AWS → Entra ID.** The workload sends that JWT to Entra ID's token endpoint as a client
   assertion, using the managed identity's **client ID**. Entra ID validates the signature and checks
   that issuer, subject, and audience exactly match a **federated identity credential** on the
   managed identity. If they match, it issues an access token for Azure Databricks.
3. **Entra ID → Databricks.** The workload calls the Databricks **SQL Statement Execution API**
   (`/api/2.0/sql/statements`) with that token. Databricks maps the token to the managed identity's
   **service principal** and enforces workspace entitlements, SQL Warehouse permissions, and Unity
   Catalog grants.

**Why outbound identity federation:** Fargate has no built-in OIDC issuer (EKS has one; ECS does
not). AWS IAM outbound identity federation turns the AWS account itself into an OIDC issuer, so the
task's IAM role can be used as an external identity.

---

## 2. Information to collect and hand off

Several values must be passed between the AWS, Azure, and Databricks configurations. Use this table
as the checklist.

| # | Value | Produced in | Consumed in | Example |
|---|---|---|---|---|
| A | AWS account ID | AWS | Azure federated credential (subject) | `123456789012` |
| B | AWS region of the ECS cluster | AWS | Task definition / logs | `us-east-2` |
| C | **AWS outbound federation issuer URL** | AWS step 3.1 | Azure federated credential (issuer) | `https://<uuid>.tokens.sts.global.api.aws` |
| D | **IAM task role ARN** | AWS step 3.2 | Azure federated credential (subject) | `arn:aws:iam::123456789012:role/ecs-<app>-databricks-prod` |
| E | Azure tenant ID | Azure | Task definition (`AZURE_TENANT_ID`) | `<tenant-guid>` |
| F | **Managed identity client ID** | Azure step 4.1 | Task definition (`AZURE_CLIENT_ID`), Databricks service principal | `<client-id-guid>` |
| G | Databricks workspace URL | Databricks | Task definition (`DATABRICKS_HOST`) | `https://adb-<workspace-id>.<n>.azuredatabricks.net` |
| H | SQL Warehouse ID | Databricks | Task definition (`DATABRICKS_WAREHOUSE_ID`) | `<16-hex-char-id>` |
| I | Egress IP(s) of the Fargate tasks | AWS networking | Databricks IP access list (only if enabled) | NAT gateway Elastic IP |

None of these values is a secret.

---

## 3. AWS configuration

### 3.1 Enable IAM outbound identity federation (account level, one time)

| Setting | Value |
|---|---|
| Where | IAM console → **Account settings** → **Outbound identity federation** → Enable (API: `EnableOutboundWebIdentityFederation`) |
| Scope | Entire AWS account (all regions) |
| Output | **Issuer URL** (value **C**), format `https://<uuid>.tokens.sts.global.api.aws` |
| Required permission for the operator | `iam:EnableOutboundWebIdentityFederation`, `iam:GetOutboundWebIdentityFederationInfo` |

After enabling, confirm that the issuer's discovery document is publicly reachable at
`<issuer URL>/.well-known/openid-configuration` (open it in a browser). It must list `RS256` among
the supported signing algorithms and point to a `jwks_uri`. Entra ID downloads the signing keys from
this public URL, so nothing has to be opened inbound.

> **Multi-account (landing zones):** the setting is **per AWS account**. Every account whose workloads
> call Azure must enable it, and each account gets its **own issuer URL**. On the Azure side, each
> federated credential therefore pairs one account's issuer with one role ARN from that account.
> The Azure tenant has no equivalent setting; one Entra ID tenant can trust any number of AWS
> accounts. This applies inside AWS Organizations too: there is no organization-level or
> management-account switch, so enable it in each member account that needs it (for example, as
> part of the landing-zone baseline). Accounts that should never federate out can be blocked with an
> SCP that denies `iam:EnableOutboundWebIdentityFederation`.

> **Important:** Enabling the feature does not by itself allow anyone to mint tokens. A principal
> also needs an IAM policy that grants `sts:GetWebIdentityToken` (step 3.2). See the guardrail in
> 3.7 to keep it that way.

### 3.2 IAM task role (the workload's identity)

This is the role the container runs as. Its ARN becomes the `sub` claim of the AWS token, and the
Azure federated credential trusts **this exact ARN**.

**Naming:** one dedicated role per workload and environment, for example
`ecs-<app>-databricks-prod`. **Create it without an IAM path** (the default `/`). If a path is used,
it becomes part of the subject (`…:role/<path>/<name>`), and the Azure federated credential must
include it exactly.

**Trust policy** — only ECS tasks from this account can assume the role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ecs-tasks.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "<AWS_ACCOUNT_ID>" },
        "ArnLike": { "aws:SourceArn": "arn:aws:ecs:<REGION>:<AWS_ACCOUNT_ID>:*" }
      }
    }
  ]
}
```

**Permissions policy** (inline or customer-managed, e.g. `allow-azure-web-identity-token`) — the role
can request tokens **only** for the Entra ID audience:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowEntraIdWebIdentityToken",
      "Effect": "Allow",
      "Action": "sts:GetWebIdentityToken",
      "Resource": "*",
      "Condition": {
        "ForAllValues:StringEquals": {
          "sts:IdentityTokenAudience": "api://AzureADTokenExchange"
        },
        "Null": { "sts:IdentityTokenAudience": "false" },
        "NumericLessThanEquals": { "sts:DurationSeconds": 300 }
      }
    }
  ]
}
```

The role needs **no other permissions** for this flow. Add the workload's own AWS permissions
separately if it needs any.

### 3.3 Task execution role

This role is used by the ECS agent, not by the application. It pulls the image and writes logs. If
the production cluster already has a standard execution role, reuse it.

| Requirement | Detail |
|---|---|
| Trust policy | `ecs-tasks.amazonaws.com` (same as 3.2) |
| Permissions | AWS managed policy `AmazonECSTaskExecutionRolePolicy` (ECR pull + CloudWatch Logs streams), or an equivalent policy scoped to the specific ECR repository and log group |
| Log group creation | The managed policy does **not** include `logs:CreateLogGroup`. Create the log group in advance (3.4). Otherwise, leave `awslogs-create-group` unset in the task definition. |
| Cross-account ECR | If the image lives in another account, that repository's policy must allow pulls from this role |

### 3.4 CloudWatch log group

| Setting | Value |
|---|---|
| Name | e.g. `/ecs/<app>-prod` |
| Retention | Per your logging standard |
| Content | The application never logs tokens. It logs the token claims (`iss`, `sub`, `aud`, `exp`), the Azure object ID and app ID, and the query results. |

### 3.5 ECS task definition

| Setting | Value |
|---|---|
| Launch type | AWS Fargate |
| OS / architecture | Linux / **X86_64** (the image must be built for `linux/amd64`; ARM64 is possible only if the image is built for it) |
| CPU / memory | 0.25 vCPU / 0.5 GB is enough for this workload |
| Network mode | `awsvpc` |
| **Task role** | The role from 3.2 — **critical**: a wrong role means a wrong token subject |
| Task execution role | The role from 3.3 |
| Container image | `<prod ECR repository URI>:<immutable tag>` |
| Port mappings | None (the container only makes outbound calls) |
| Health check | None needed for a run-to-completion task |
| Logging | `awslogs` → log group from 3.4, region **B** |
| Environment variables | See table below (none are secrets, so they don't need Secrets Manager) |

| Environment variable | Value | Notes |
|---|---|---|
| `AZURE_TENANT_ID` | value **E** | |
| `AZURE_CLIENT_ID` | value **F** | Managed identity **client ID**, not its principal/object ID |
| `DATABRICKS_HOST` | value **G** | Workspace URL |
| `DATABRICKS_WAREHOUSE_ID` | value **H** | |
| `AZURE_SCOPE` | *(optional)* default `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default` | Well-known Azure Databricks resource ID, the same in every tenant. Don't change it. |
| `AWS_TOKEN_AUDIENCE` | *(optional)* default `api://AzureADTokenExchange` | Must match both the IAM condition (3.2) and the federated credential audience (4.2) |

`AWS_REGION` is set by Fargate automatically.

The task runs to completion: it exits with code `0` when all queries succeed, and `1` otherwise. Run
it as a standalone or scheduled task (for example, from an EventBridge Scheduler schedule), **not**
as an ECS service. A service would restart it continuously.

### 3.6 Networking

The task only needs **outbound HTTPS (TCP 443)**. It doesn't need any inbound rules.

| Destination | Purpose |
|---|---|
| `sts.<region>.amazonaws.com` | Get the AWS identity token |
| ECR (`api.ecr`, `dkr.ecr`) and S3 in the region | Image pull |
| `logs.<region>.amazonaws.com` | CloudWatch Logs |
| `login.microsoftonline.com` | Entra ID token exchange |
| `<workspace>.azuredatabricks.net` (value **G**) | Databricks SQL API |

**Recommendation for production:** run the tasks in **private subnets** with a **NAT gateway**
(public IP disabled). Optionally, use VPC endpoints for ECR, S3, CloudWatch Logs, and STS. If you
use an STS VPC endpoint, confirm that `GetWebIdentityToken` works through it before you rely on it.
Entra ID and Databricks are internet endpoints and must go through the NAT.

If the Databricks workspace has **IP access lists** enabled, add the NAT gateway's Elastic IP(s)
(value **I**). A task with a public IP gets a different address on every run, so it can't be
allow-listed reliably. If the workspace is **Private Link only**, you need private connectivity from
the AWS VPC to Azure (VPN or ExpressRoute), which is out of scope for this guide.

### 3.7 Guardrails (recommended)

**Restrict who can mint outbound tokens.** Once outbound federation is enabled, any principal with
`sts:GetWebIdentityToken` in an IAM policy can obtain tokens for this account's issuer. If your
organization uses AWS Organizations, add an SCP that denies the action to every role except the
approved workload roles:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyWebIdentityTokenExceptApprovedRoles",
      "Effect": "Deny",
      "Action": "sts:GetWebIdentityToken",
      "Resource": "*",
      "Condition": {
        "ArnNotLike": {
          "aws:PrincipalArn": [
            "arn:aws:iam::<AWS_ACCOUNT_ID>:role/ecs-<app>-databricks-prod"
          ]
        }
      }
    }
  ]
}
```

**Check your existing SCPs.** If the organization already has SCPs, they must allow
`sts:GetWebIdentityToken` for the task role and `iam:EnableOutboundWebIdentityFederation` for the
operator who performs step 3.1. In the POC account, an SCP restricted ECS and ECR to a single
region, which also determines where the cluster and repository can live.

**Restrict who can edit the task role.** Anyone who can change the task role's trust policy, or
attach it to another task definition, can act as the managed identity in Azure. Limit `iam:Update*`
on the role, and limit `iam:PassRole` for it to the deployment pipeline.

**Audit.** `GetWebIdentityToken` calls are recorded in CloudTrail.

---

## 4. Azure configuration

### 4.1 User-assigned managed identity

| Setting | Value |
|---|---|
| Where | Azure portal → **Managed Identities** → Create |
| Subscription / resource group | Your production subscription and resource group |
| Region | Any; it doesn't affect authentication |
| Name | e.g. `id-<app>-databricks-prod` |
| Outputs | **Client ID** (value **F**), **Object (principal) ID**, **Tenant ID** (value **E**) |

**Why a user-assigned managed identity rather than an app registration?** It has no secrets or
certificates that could ever be added to it. It's a standalone Azure resource with its own RBAC and
lifecycle, and it supports federated identity credentials.

**No Azure RBAC role assignments are needed for this flow.** Databricks data access is authorized
inside Databricks (section 5), not through Azure RBAC. Add Azure roles only if the workload also
manages Azure resources.

### 4.2 Federated identity credential

| Setting | Value |
|---|---|
| Where | Managed identity → **Settings → Federated credentials** → **Add credential** |
| Scenario | **Other issuer** |
| Issuer | Value **C**, exactly — `https`, no trailing slash |
| Subject identifier | Value **D**, exactly — the **role ARN** `arn:aws:iam::<account>:role/<role-name>`, **not** the assumed-role session ARN (`arn:aws:sts::…:assumed-role/…`) |
| Audience | `api://AzureADTokenExchange` |
| Name | e.g. `aws-ecs-<app>-prod` |

**Matching rules:** issuer, subject, and audience are compared **exactly** (case-sensitive, no
wildcards).
- One credential trusts exactly one IAM role. If the role is renamed or recreated in another
  account, the credential must be updated.
- A managed identity can have up to 20 federated credentials.
- Use separate managed identities for separate environments. Don't add non-prod roles to the prod
  identity.
- New or changed credentials can take a minute or two to take effect.

### 4.3 Guardrails (recommended)

- **Restrict who can manage the identity.** Anyone with write access to the managed identity
  (for example **Managed Identity Contributor**, Contributor, or Owner on its resource group) can add
  a federated credential and so grant another external workload access. Keep that access limited to
  the platform team.
- **Audit.** Token exchanges appear in Entra ID **Sign-in logs → Managed identity sign-ins**.
  Changes to federated credentials appear in the Azure Activity Log.

---

## 5. Azure Databricks configuration

All steps are done by a Databricks **account admin** or **workspace admin**.

### 5.1 Register the managed identity as a service principal

| Setting | Value |
|---|---|
| Where (Unity Catalog workspaces, recommended) | **Account console → User management → Service principals → Add service principal**, then **Workspaces → \<workspace\> → Permissions → Add** it with the **User** role |
| Where (workspace level, alternative) | **Workspace → Settings → Identity and access → Service principals → Add → Microsoft Entra ID managed** |
| Management | **Microsoft Entra ID managed** |
| Application ID | Value **F** (the managed identity's **client ID**) |
| Display name | Same as the managed identity name |

When it runs, `SELECT current_user()` returns this application ID. This confirms that Databricks
sees the managed identity.

### 5.2 Workspace entitlements

| Entitlement | Setting |
|---|---|
| **Databricks SQL access** | **Enabled** (required to use SQL Warehouses) |
| Workspace access | Not required for this workload; disable it if your policy is least privilege |
| Allow cluster creation | Not required |

### 5.3 SQL Warehouse permission

| Setting | Value |
|---|---|
| Where | **SQL Warehouses → \<warehouse\> → Permissions** |
| Permission | **Can use** |
| Warehouse ID | Value **H** — the last segment of the warehouse's **Connection details → HTTP path** (`/sql/1.0/warehouses/<id>`) |

Consider a serverless warehouse with auto-stop. With a stopped classic or pro warehouse, the first
query waits for the warehouse to start, which can take several minutes. The application waits up to
5 minutes per query.

### 5.4 Unity Catalog data permissions

Grant only what the workload needs, to the service principal (identified by its application ID):

| Securable | Privilege |
|---|---|
| Catalog | `USE CATALOG` |
| Schema | `USE SCHEMA` |
| Tables / views read | `SELECT` |

Equivalent SQL, run by the data owner in a Databricks SQL editor:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<managed-identity-client-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<managed-identity-client-id>`;
GRANT SELECT ON TABLE <catalog>.<schema>.<table> TO `<managed-identity-client-id>`;
```

The POC queried the built-in `samples.wanderbricks` dataset, which every workspace user can read.
Production tables need explicit grants.

### 5.5 Workspace network access

Only needed if enabled on the workspace: add the Fargate egress IP(s) (value **I**) to the
workspace **IP access list**. See 3.6.

---

## 6. Application and image

| Item | Detail |
|---|---|
| Code | [app/main.py](app/main.py): Python 3.12, using `boto3` (AWS token), `azure-identity` (`ClientAssertionCredential` for the Entra ID exchange), and `requests` (Databricks SQL Statement Execution API). Dependencies are pinned in [app/requirements.txt](app/requirements.txt). |
| Queries | Hardcoded in `QUERIES` in `app/main.py` |
| Image | [Dockerfile](Dockerfile): `python:3.12-slim`, runs as a non-root user (UID 10001). Must be built for **linux/amd64** to match the task definition. |
| Registry | Push to the existing production ECR repository with an **immutable tag** (e.g. a version or git SHA). Don't use `latest`. Enabling ECR image scanning is recommended. |
| Release | Each new image tag needs a new **task definition revision** that references it |
| Environment values | The application's settings (tenant, client ID, workspace URL, warehouse ID) are environment variables. For local runs, keep them in a `.env` file, which is git-ignored and excluded from the image. The committed template is [.env.example](.env.example). |
| Credentials | None in the image or task definition. The AWS credentials come from the task role at runtime. Entra ID tokens are requested at runtime and never stored. |
| Token lifetimes | AWS JWT: 5 minutes (used once, for the exchange). Entra ID token for Databricks: about 24 hours for managed identities (in the POC, the token was valid for 24 h). Each task run gets fresh tokens. |

---

## 7. Validation

Run the task once and check the CloudWatch log stream. A successful run produces the lines below.
Each line confirms one part of the configuration:

| Log line | Confirms |
|---|---|
| `AWS caller identity: arn:aws:sts::<account>:assumed-role/<task-role>/<task-id>` | Task role attached correctly (3.2, 3.5) |
| `AWS token claims -> iss=https://<id>.tokens.sts.global.api.aws` | Outbound federation enabled; issuer = value **C** (3.1) |
| `AWS token claims -> sub=arn:aws:iam::<account>:role/<task-role>` | Subject = value **D** (3.2) |
| `AWS token claims -> aud=api://AzureADTokenExchange` | IAM audience condition OK (3.2) |
| `Azure token acquired … aud=2ff814a6-… appid=<client-id>` | Federated credential matches; token is for Azure Databricks (4.2) |
| `Query 'Who am I' -> 1 rows` with `identity = <client-id>` | Service principal registered, SQL entitlement and warehouse permission OK (5.1–5.3) |
| `Query '<name>' -> N rows` for the data queries | Unity Catalog grants OK (5.4) |
| `SUCCESS: N/N queries ran on Databricks via workload identity federation` and exit code `0` | End to end |

After the container exits, ECS needs another 20–30 seconds to deprovision the task, so it shows as
`STOPPED` shortly after the `SUCCESS` line.

---

## 8. Troubleshooting

| Symptom (in the task logs or stopped reason) | Cause / fix |
|---|---|
| `AccessDenied` on `GetWebIdentityToken` | Task role is missing the policy from 3.2, the audience doesn't match the condition, or an SCP denies the action (3.7) |
| `FeatureDisabled` / outbound federation disabled | Step 3.1 not done in this account |
| `AADSTS700211` (no matching federated identity record for the **issuer**) | The issuer in 4.2 differs from the logged `iss` (check for a trailing slash, or a different AWS account) |
| `AADSTS700213` (no matching federated identity record for the **subject**) | The subject in 4.2 differs from the logged `sub`: wrong task role, a role with an IAM path, or the session ARN used instead of the role ARN |
| `AADSTS70021` (no matching federated identity record) | Issuer, subject, or audience mismatch. Compare all three with the logged claims. |
| `AADSTS700016` / `AADSTS700038` (application not found / invalid identifier) | `AZURE_CLIENT_ID` is wrong (it must be the client ID, not the object ID) or `AZURE_TENANT_ID` is wrong |
| `Missing required env vars: …` | Variables missing from the task definition revision (3.5) |
| Databricks `HTTP 401` | The managed identity isn't registered as a service principal in this workspace (5.1), or `AZURE_SCOPE` was overridden |
| Databricks `HTTP 403` / `PERMISSION_DENIED` | **Databricks SQL access** entitlement (5.2) or **Can use** on the warehouse (5.3) is missing, or an IP access list is blocking the egress IP (5.5) |
| Query `FAILED: … INSUFFICIENT_PERMISSIONS` / `TABLE_OR_VIEW_NOT_FOUND` | Unity Catalog grants missing (5.4), or wrong table name |
| `timed out after 300s` | Warehouse stopped and slow to start, or auto-start disabled |
| Connection timeouts to Entra ID or Databricks | No outbound route (private subnet without NAT), a security group or NACL blocking 443, or a Private Link-only workspace (3.6) |
| `CannotPullContainerError` | No route to ECR, execution role lacks pull permission, or wrong image URI/tag (3.3) |
| `ResourceInitializationError` about logs | Log group doesn't exist, or its name/region differs from the task definition (3.4) |
| `exec format error` | Image built for the wrong CPU architecture. Rebuild for `linux/amd64`. |

---

## 9. Production checklist

**AWS**
- [ ] Outbound identity federation enabled; issuer URL (C) recorded; discovery document reachable
- [ ] Task role created without an IAM path; trust policy restricted with `aws:SourceAccount` and `aws:SourceArn`; `sts:GetWebIdentityToken` policy with the audience condition; ARN (D) recorded
- [ ] Execution role able to pull from the production ECR repository and write to the log group
- [ ] Log group created, with retention set
- [ ] Task definition: correct task role, X86_64, four environment variables, immutable image tag
- [ ] Private subnets with NAT; outbound 443 allowed; NAT Elastic IP (I) recorded if needed
- [ ] SCP guardrail limiting `sts:GetWebIdentityToken` to approved roles

**Azure**
- [ ] User-assigned managed identity created; client ID (F) and tenant ID (E) recorded
- [ ] Federated credential: issuer = C, subject = D, audience = `api://AzureADTokenExchange`
- [ ] Write access to the managed identity limited to the platform team

**Databricks**
- [ ] Managed identity added as a Microsoft Entra ID-managed service principal (application ID = F) and assigned to the workspace
- [ ] **Databricks SQL access** entitlement enabled
- [ ] **Can use** on the SQL Warehouse; warehouse ID (H) recorded
- [ ] Unity Catalog `USE CATALOG` / `USE SCHEMA` / `SELECT` on the required objects
- [ ] IP access list updated with the NAT IP (if IP access lists are used)

**Validation**
- [ ] One task run shows every log line in section 7 and exits with code `0`
