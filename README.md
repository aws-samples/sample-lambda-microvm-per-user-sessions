# Per-user VS Code in the browser with AWS Lambda MicroVMs and Amazon CloudFront

This sample gives every signed-in user their own VS Code in the browser,
running in their own [AWS Lambda MicroVM](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html).
Each MicroVM is a separate Firecracker-isolated environment, so users don't
share compute, files, or terminals. When a user goes idle, the MicroVM is
suspended rather than destroyed, and it resumes in place with files and running
processes intact. Amazon CloudFront adds the MicroVM auth header for the
browser, so you don't need an always-on proxy in front of the environments.

Use it as a starting point for per-user cloud IDEs, workshops, or sandboxes for
coding agents and untrusted or AI-generated code.

> **This is sample code, for non-production usage.** You should work with your
> security and legal teams to meet your organizational security, regulatory and
> compliance requirements before deployment. See
> [Before you use this in production](#before-you-use-this-in-production).

## Contents

- [Architecture](#architecture)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Costs](#costs)
- [Security model](#security-model)
- [Project layout](#project-layout)
- [Cleanup](#cleanup)
- [Related resources](#related-resources)

## Architecture

![Architecture diagram. A browser signs in with Amazon Cognito and loads the start page from Amazon S3 through the site CloudFront distribution. The start page calls the session API, an HTTP API with a Cognito JWT authorizer in front of a Lambda function that stores state in Amazon DynamoDB, launches the user's Lambda MicroVM, and returns a one-time launch ticket. The browser redeems the ticket on a separate IDE CloudFront distribution, where a CloudFront Function routes every /ide/ request to the user's own MicroVM and adds the MicroVM auth header.](docs/architecture.svg)

Every request to a MicroVM endpoint must carry an `x-aws-proxy-auth` header.
Browsers can't add custom headers to page loads, asset requests, or WebSocket
upgrades, so VS Code can't be opened straight from the MicroVM endpoint. A
[CloudFront Function](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/helper-functions-origin-modification.html)
on `/ide/*` solves that: it sends each request to the signed-in user's MicroVM
and adds the header. VS Code's HTML, assets, and WebSocket all go through it.

VS Code runs on its own hostname, separate from the start page. Anything running
inside a MicroVM can serve content under `/ide/`, so the sample treats the IDE
as untrusted. Because it's a different origin, it can't read the user's Cognito
tokens or call the session API with them.

How a session works:

1. The start page signs the user in with the OAuth 2.0 authorization code flow
   with PKCE against Amazon Cognito managed login. Self sign-up is off, so an
   administrator decides who can launch MicroVMs (and who pays for them).
2. The page calls `POST /api/session` with the Cognito access token. The session
   Lambda function looks up the caller's MicroVM in Amazon DynamoDB, launches
   one if there is none, and returns a random launch ticket. The ticket works
   once and expires after 60 seconds.
3. The page opens `<IdeUrl>/launch?t=<ticket>`. The session function redeems the
   ticket, mints a MicroVM auth token (port 8080, 60 minutes), and sets the
   endpoint and token as `HttpOnly` cookies on the IDE host, scoped to `/ide`.
   Then the browser opens `/ide/`.
4. For every request to `/ide/*`, the CloudFront Function turns the cookies into
   the origin and the auth header.
5. When the token and its cookie expire, the CloudFront Function redirects to
   the start page with `?session=expired`. The start page gets a fresh ticket
   and sends the user back to the IDE.

## Getting started

### Prerequisites

- An AWS account and credentials with permission to deploy the stack
- A Region where Lambda MicroVMs is available (see
  [AWS services by Region](https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/))
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs a
  matching Python 3.10+ if you don't have one)
- Node.js 18+ for the AWS CDK CLI
- The AWS CLI, to create users and for cleanup
- AWS CDK bootstrapped in the target account and Region (`npx aws-cdk bootstrap`)

### Deploy

Clone this repository and deploy:

```bash
git clone https://github.com/aws-samples/sample-lambda-microvm-per-user-sessions.git
cd sample-lambda-microvm-per-user-sessions
uv sync

# The Cognito domain prefix must be globally unique.
npx aws-cdk deploy -c cognitoDomainPrefix=<your-unique-prefix>
```

`uv sync` creates `.venv` with the pinned dependencies from `uv.lock`, and the
CDK runs the app through `uv run`. The stack builds the MicroVM image from
`microvm/` during the deployment. The image build starts VS Code and snapshots
it once it serves its workbench, so every user's MicroVM starts with VS Code
already running. The first deploy takes several minutes. Note the `SiteUrl`,
`IdeUrl`, and `UserPoolId` outputs.

Deploying again after changing `microvm/` builds a new image version. New
MicroVMs launch on it, and existing ones keep their version until they
terminate.

### Create a user

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true
```

Cognito emails a temporary password. The user sets a new one on first sign-in.

### Try it

1. Open `SiteUrl`, choose **Sign in**, then **Open VS Code**. VS Code opens on
   the `IdeUrl` host. The first start launches your MicroVM; later starts
   reconnect to the same one.
2. Open a terminal (**Terminal > New Terminal**) and create a file:
   `echo hello > hello.txt`.
3. Close the tab. After the idle timeout your MicroVM is suspended (or suspend
   it yourself with the `SuspendMicrovm` API). Open `SiteUrl` again and choose
   **Open VS Code**: the MicroVM resumes and `hello.txt` is still there.
4. Sign in as a second user in another browser profile: that user gets a
   different MicroVM and doesn't see your files.
5. **Terminate** on the start page ends your MicroVM immediately.

## Configuration

Lifecycle defaults, set in `cdk.json`:

| Setting | Default | Effect |
|---|---|---|
| `tokenMinutes` | 60 | Lifetime of each MicroVM auth token and its cookie (max 60) |
| `idleSeconds` | 600 | Suspend a MicroVM after 10 minutes without traffic |
| `suspendedSeconds` | 3600 | Terminate a suspended MicroVM after 1 hour |
| `maxLifetimeSeconds` | 14400 | Hard lifetime of any MicroVM (service max: 8 hours) |

While VS Code is open, its WebSocket keeps the MicroVM active. Idle suspend
starts once the user closes the tab.

Files live on the MicroVM's disk. They survive suspend and resume, and they're
deleted when the MicroVM terminates. Keep work in a Git remote, or extend the
sample with Amazon S3 or Amazon EFS if you need durable storage.

## Costs

You pay for each MicroVM while it runs. Suspended MicroVMs and stored image
versions have their own charges. The other services in the stack (CloudFront,
Amazon API Gateway, Lambda, DynamoDB, S3, Cognito) are billed per request or per user
and cost little at sample scale. Check the
[AWS Lambda pricing page](https://aws.amazon.com/lambda/pricing/) for your Region
before you deploy, keep the lifecycle limits in [Configuration](#configuration)
short, and run the [cleanup](#cleanup) steps when you're done.

## Security model

| Control | Where |
|---|---|
| Only users an admin creates can sign in | Cognito `AllowAdminCreateUserOnly` |
| Session API requires a valid Cognito access token | HTTP API JWT authorizer |
| One MicroVM per user, identity from the validated `sub` claim | `backend/session/app.py` |
| MicroVM tokens are per-MicroVM, port-scoped, 60-minute lifetime | `CreateMicrovmAuthToken` |
| Code running in a MicroVM can't read the user's Cognito tokens or call the session API | IDE on its own CloudFront hostname, a different origin from the start page |
| Launch tickets are random, single use, valid for 60 seconds, and minted only for a caller with a valid Cognito token | `backend/session/app.py` (`issue_ticket`, `redeem_ticket`) |
| Page JavaScript can't read the MicroVM token | `HttpOnly; Secure; SameSite=Strict; Path=/ide` cookies, set on the IDE host only |
| CloudFront only routes `/ide` paths, and only to MicroVM endpoints in this Region | path check and endpoint allowlist regex in `infra/ide_router.js` |
| Viewer-supplied `x-aws-proxy-*` headers are dropped; session cookies are not forwarded | `infra/ide_router.js` |
| The start page may only connect to itself and Cognito | CloudFront Content-Security-Policy |
| VS Code runs as an unprivileged user | `microvm/Dockerfile` |

Design notes:

- VS Code runs without its own connection token. Port 8080 is only reachable
  through the MicroVM proxy, and the proxy requires a token that only the user's
  browser (in an `HttpOnly` cookie) and CloudFront (per request) hold.
- Workspace trust is off. Each workspace belongs to one user who controls
  everything in it, so VS Code's Restricted Mode prompt doesn't protect anything
  here. Turn it back on in `microvm/settings/machine-settings.json` if your
  users open code from untrusted sources.
- The MicroVM token is a bearer credential for one MicroVM until it expires.
  Anyone who gets a user's cookies can reach that user's VS Code, so keep the
  lifetime short for sensitive use.
- Code in a MicroVM controls that user's session on the IDE host. It can change
  what the user sees under `/ide/`, including showing a fake prompt. It can't
  reach the start page, the Cognito tokens, or other users' MicroVMs. Tell users
  to sign in only on the start page, and keep the two hostnames separate if you
  add custom domains.
- MicroVMs reach the internet through the default egress connector, so users
  can install extensions and packages. Attach a VPC egress connector if you need
  to restrict that.

### Before you use this in production

The sample keeps the stack small on purpose. For production, also consider:

- AWS WAF on the CloudFront distributions and throttling on the HTTP API stage,
  so a signed-in user can't call the session API at high rates.
- Access logging for CloudFront and the HTTP API, and a budget alarm for
  MicroVM runtime cost.
- Custom domains with a TLS 1.2+ viewer security policy (the default
  `*.cloudfront.net` certificate doesn't let you set the minimum protocol). Use
  separate hostnames for the start page and the IDE, for example
  `code.example.com` and `ide.example.com`, and keep the session cookies
  host-only (no `Domain` attribute).
- Multi-factor authentication in the Cognito user pool.
- A VPC egress connector to restrict outbound traffic from the MicroVMs.
- AWS KMS encryption for CloudWatch log groups if your policies require it. The
  session function's environment variables hold no secrets.
- The `BucketDeployment` construct that uploads the start page creates a role
  with `cloudfront:CreateInvalidation` on `*`. Replace it with your own
  deployment pipeline if you need tighter scoping.

## Project layout

| Path | What it is |
|---|---|
| `infra/stack.py` | CDK stack: MicroVM image, Cognito, HTTP API, session function, DynamoDB, two CloudFront distributions + S3 |
| `infra/ide_router.js` | CloudFront Function that routes `/ide/*` to the caller's MicroVM |
| `backend/session/app.py` | Session function: find or launch the caller's MicroVM, issue and redeem launch tickets, mint its token, set cookies |
| `microvm/` | MicroVM image: Dockerfile, VS Code start script, lifecycle hooks, workspace template |
| `frontend/` | Start page: PKCE sign-in, session call, hand-over to the IDE host, terminate |
| `docs/` | Architecture diagram (`architecture.svg`) |
| `pyproject.toml`, `uv.lock` | Python dependencies for the CDK app, pinned for `uv sync` |

The session function calls the Lambda MicroVMs API with SigV4-signed HTTPS
requests, so it doesn't depend on the boto3 version bundled with the Lambda
runtime.

## Cleanup

Terminate running MicroVMs first (each user can choose **Terminate**, or use the
Lambda console), then:

```bash
npx aws-cdk destroy -c cognitoDomainPrefix=<your-unique-prefix>
```

## Related resources

- [AWS Lambda MicroVMs documentation](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html)
- [CloudFront Functions: origin modification helper methods](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/helper-functions-origin-modification.html)
- [Amazon Cognito managed login](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-managed-login.html)

## Third-party dependencies

Building and deploying this sample downloads the following. Review their
licenses before use.

| Dependency | License | Source | Used by |
|---|---|---|---|
| [openvscode-server](https://github.com/gitpod-io/openvscode-server) v1.109.5 (pinned by SHA-256) | MIT | GitHub releases | `microvm/Dockerfile` |
| `public.ecr.aws/lambda/microvms:al2023-minimal` base image | see image | Amazon ECR Public | `microvm/Dockerfile` |
| `git`, `python3`, and basic tools | various | Amazon Linux 2023 package repositories | `microvm/Dockerfile` |
| `aws-cdk-lib`, `constructs` and their dependencies | Apache-2.0, MIT, BSD-3-Clause, PSF-2.0 | PyPI | `pyproject.toml` |
| AWS CDK CLI (`aws-cdk`) | Apache-2.0 | npm | deployment |

VS Code extensions that users install come from the Open VSX registry under
their own licenses. See [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) for the
full list and license links.

## Security

See [SECURITY](SECURITY.md) for how to report a security issue.

## License

This sample is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
