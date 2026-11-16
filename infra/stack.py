# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Per-user VS Code in the browser, each running in its own AWS Lambda MicroVM.

Two CloudFront distributions, so the IDE never shares an origin with the
start page (code in a MicroVM can then never read the user's Cognito tokens):

Site distribution
  /        static start page (S3), Cognito sign-in
  /api/*   session API (HTTP API + Cognito JWT authorizer): finds or launches the
           caller's MicroVM and returns a one-time launch ticket
IDE distribution
  /launch  redeems the ticket and sets HttpOnly cookies with the MicroVM endpoint
           and a short-lived MicroVM auth token, on the IDE host only
  /ide/*   VS Code. A CloudFront Function sends each request to the caller's
           MicroVM and adds the x-aws-proxy-auth header the browser can't send.
"""
import json
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent

# Lifecycle hooks the workspace implements on port 9000 (see microvm/hooks.py).
HOOKS = lambda_.CfnMicrovmImage.HooksProperty(
    port=9000,
    microvm_image_hooks=lambda_.CfnMicrovmImage.MicrovmImageHooksProperty(
        ready="ENABLED", ready_timeout_in_seconds=60,
    ),
    microvm_hooks=lambda_.CfnMicrovmImage.MicrovmHooksProperty(
        run="ENABLED", run_timeout_in_seconds=5,
        resume="ENABLED", resume_timeout_in_seconds=5,
        suspend="ENABLED", suspend_timeout_in_seconds=5,
        terminate="ENABLED", terminate_timeout_in_seconds=5,
    ),
)


class MicrovmSessionsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = self.node.try_get_context("config") or {}
        domain_prefix = self.node.try_get_context("cognitoDomainPrefix")
        if not domain_prefix:
            raise ValueError("Pass a globally unique Cognito domain prefix: "
                             "cdk deploy -c cognitoDomainPrefix=<your-prefix>")
        image_name = f"{construct_id.lower()}-workspace"
        region, account = self.region, self.account

        # ------------------------------------------------------------------
        # MicroVM image: microvm/ is zipped as an S3 asset and built by the
        # service. Changing the code produces a new image version on deploy.
        # ------------------------------------------------------------------
        def microvm_role(name: str) -> iam.Role:
            return iam.Role(
                self, name,
                assumed_by=iam.ServicePrincipal(
                    "lambda.amazonaws.com",
                    conditions={
                        "StringEquals": {"aws:SourceAccount": account},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:lambda:{region}:{account}:microvm-image:*"},
                    },
                ),
            )

        workspace_logs = logs.LogGroup(
            self, "WorkspaceLogs",
            log_group_name=f"/aws/lambda-microvms/{image_name}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        build_role = microvm_role("MicrovmBuildRole")
        workspace_logs.grant_write(build_role)

        # The workspace calls no AWS APIs, so its execution role only needs to write logs.
        execution_role = microvm_role("MicrovmExecutionRole")
        workspace_logs.grant_write(execution_role)

        source = s3_assets.Asset(self, "WorkspaceSource", path=str(ROOT / "microvm"),
                                 exclude=["**/__pycache__"])
        source.grant_read(build_role)

        image = lambda_.CfnMicrovmImage(
            self, "WorkspaceImage",
            name=image_name,
            description="Per-user VS Code (openvscode-server)",
            base_image_arn=f"arn:aws:lambda:{region}:aws:microvm-image:al2023-1",
            base_image_version="1",
            build_role_arn=build_role.role_arn,
            code_artifact=lambda_.CfnMicrovmImage.CodeArtifactProperty(uri=source.s3_object_url),
            cpu_configurations=[lambda_.CfnMicrovmImage.CpuConfigurationProperty(architecture="ARM_64")],
            resources=[lambda_.CfnMicrovmImage.ResourcesProperty(minimum_memory_in_mib=4096)],
            hooks=HOOKS,
            logging=lambda_.CfnMicrovmImage.LoggingProperty(
                cloud_watch=lambda_.CfnMicrovmImage.CloudWatchLoggingProperty(
                    log_group=workspace_logs.log_group_name),
            ),
            additional_os_capabilities=[],
            egress_network_connectors=[],
            environment_variables=[],
        )
        image.node.add_dependency(build_role)

        # ------------------------------------------------------------------
        # Static site: private S3 bucket behind CloudFront (origin access control).
        # ------------------------------------------------------------------
        site_bucket = s3.Bucket(
            self, "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        cognito_domain_url = f"https://{domain_prefix}.auth.{region}.amazoncognito.com"
        csp = "; ".join([
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            # The start page only talks to its own origin (/api) and Cognito.
            f"connect-src 'self' {cognito_domain_url}",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ])
        headers = cloudfront.ResponseHeadersPolicy(
            self, "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True),
            ),
        )
        # The session API sits behind both distributions: /api/* on the site,
        # /launch on the IDE host. Routes are added further down, once the
        # Cognito client (whose callback URL needs the site domain) exists.
        api = apigw.HttpApi(self, "SessionApi", create_default_stage=True)
        api_origin = origins.HttpOrigin(f"{api.api_id}.execute-api.{region}.{self.url_suffix}")
        dynamic = dict(
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            # Host must be the origin's own name, so forward everything else.
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        )
        # CloudFront forwards the Authorization header only if it is part of the
        # cache key. The API responses are no-store, so nothing is cached.
        api_cache = cloudfront.CachePolicy(
            self, "ApiCachePolicy",
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list("Authorization"),
            default_ttl=Duration.seconds(0), min_ttl=Duration.seconds(0), max_ttl=Duration.seconds(1),
        )
        distribution = cloudfront.Distribution(
            self, "Site",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(origin=api_origin, **{**dynamic, "cache_policy": api_cache}),
            },
        )
        site_url = f"https://{distribution.distribution_domain_name}"

        # ------------------------------------------------------------------
        # IDE distribution: its own hostname, so the IDE is a different origin
        # from the start page. Anything a MicroVM serves under /ide/ can't read
        # the start page's storage or call the session API with its tokens.
        # ------------------------------------------------------------------
        ide_router = cloudfront.Function(
            self, "IdeRouter",
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            code=cloudfront.FunctionCode.from_inline(
                (ROOT / "infra" / "ide_router.js").read_text()
                .replace("__REGION__", region).replace("__SITE_URL__", site_url)),
            comment="Route /ide/* to the caller's MicroVM with its auth token",
        )
        # No CSP here: VS Code sets its own. Frames stay same-origin for webviews.
        ide_headers = cloudfront.ResponseHeadersPolicy(
            self, "IdeSecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.SAMEORIGIN, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True),
            ),
        )
        ide_distribution = cloudfront.Distribution(
            self, "Ide",
            comment="Per-user VS Code (IDE host)",
            # The origin here is only a placeholder: ide_router replaces it
            # with the caller's MicroVM endpoint on every request.
            default_behavior=cloudfront.BehaviorOptions(
                origin=api_origin,
                function_associations=[cloudfront.FunctionAssociation(
                    function=ide_router, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)],
                response_headers_policy=ide_headers,
                **dynamic,
            ),
            additional_behaviors={
                "/launch": cloudfront.BehaviorOptions(
                    origin=api_origin, response_headers_policy=ide_headers, **dynamic),
            },
        )
        ide_url = f"https://{ide_distribution.distribution_domain_name}"

        # ------------------------------------------------------------------
        # Sign-in: Cognito user pool with managed login. Self sign-up is off,
        # so only users an administrator creates can launch MicroVMs.
        # ------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_lowercase=True, require_uppercase=True,
                require_digits=True, require_symbols=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        client = user_pool.add_client(
            "WebClient",
            generate_secret=False,  # browser app: authorization code flow with PKCE
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{site_url}/"],
                logout_urls=[f"{site_url}/"],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
            refresh_token_validity=Duration.days(1),
        )
        user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        cognito.CfnManagedLoginBranding(
            self, "LoginBranding",
            user_pool_id=user_pool.user_pool_id,
            client_id=client.user_pool_client_id,
            use_cognito_provided_values=True,
        )

        # ------------------------------------------------------------------
        # Session API: validates the Cognito access token, then finds or
        # launches the caller's MicroVM and mints a short-lived auth token,
        # returned as HttpOnly cookies.
        # ------------------------------------------------------------------
        sessions = dynamodb.TableV2(
            self, "Sessions",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expiresAt",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # One-time launch tickets: hand a validated session from the start page
        # to the IDE host. Single use, 60 seconds, no secrets stored.
        tickets = dynamodb.TableV2(
            self, "LaunchTickets",
            partition_key=dynamodb.Attribute(name="ticket", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expiresAt",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.DESTROY,
        )

        session_fn = lambda_.Function(
            self, "SessionFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="app.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "backend" / "session")),
            memory_size=256,
            timeout=Duration.seconds(29),
            log_group=logs.LogGroup(self, "SessionFunctionLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
            environment={
                "SESSIONS_TABLE": sessions.table_name,
                "TICKETS_TABLE": tickets.table_name,
                "SITE_URL": site_url,
                "IMAGE_ARN": image.attr_image_arn,
                "IMAGE_VERSION": image.attr_latest_active_image_version,
                "EXECUTION_ROLE_ARN": execution_role.role_arn,
                "LOG_GROUP": workspace_logs.log_group_name,
                "TOKEN_MINUTES": str(cfg.get("tokenMinutes", 60)),
                "IDLE_SECONDS": str(cfg.get("idleSeconds", 600)),
                "SUSPENDED_SECONDS": str(cfg.get("suspendedSeconds", 3600)),
                "MAX_LIFETIME_SECONDS": str(cfg.get("maxLifetimeSeconds", 14400)),
            },
        )
        sessions.grant_read_write_data(session_fn)
        tickets.grant_read_write_data(session_fn)
        execution_role.grant_pass_role(session_fn)
        # MicroVM actions are authorized against the image they run from, so
        # the session Lambda can only launch and manage MicroVMs of this image.
        session_fn.add_to_role_policy(iam.PolicyStatement(
            sid="MicrovmControlPlane",
            actions=["lambda:RunMicrovm", "lambda:GetMicrovm",
                     "lambda:TerminateMicrovm", "lambda:CreateMicrovmAuthToken"],
            resources=[image.attr_image_arn],
        ))
        session_fn.add_to_role_policy(iam.PolicyStatement(
            sid="PassDefaultNetworkConnectors",
            actions=["lambda:PassNetworkConnector"],
            resources=[f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:*"],
        ))

        api.add_routes(
            path="/api/session",
            methods=[apigw.HttpMethod.POST, apigw.HttpMethod.DELETE],
            integration=integrations.HttpLambdaIntegration("Session", session_fn),
            authorizer=authorizers.HttpUserPoolAuthorizer("Cognito", user_pool,
                                                          user_pool_clients=[client]),
        )
        # No JWT here: the IDE host never holds Cognito tokens. The single-use
        # ticket, minted only behind the JWT authorizer above, is the credential.
        api.add_routes(
            path="/launch",
            methods=[apigw.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("Launch", session_fn),
        )

        # ------------------------------------------------------------------
        # Upload the site, with config.js rendered from this stack's values.
        # ------------------------------------------------------------------
        config_js = "window.APP_CONFIG = " + json.dumps({
            "cognitoDomain": cognito_domain_url,
            "clientId": client.user_pool_client_id,
            "redirectUri": f"{site_url}/",
            "ideUrl": ide_url,
        }) + ";\n"
        s3deploy.BucketDeployment(
            self, "DeploySite",
            destination_bucket=site_bucket,
            sources=[
                s3deploy.Source.asset(str(ROOT / "frontend"), exclude=["config.js"]),
                s3deploy.Source.data("config.js", config_js),
            ],
            distribution=distribution,
        )

        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "IdeUrl", value=ide_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "ImageArn", value=image.attr_image_arn)
        CfnOutput(self, "ImageVersion", value=image.attr_latest_active_image_version)
        CfnOutput(self, "SessionsTableName", value=sessions.table_name)
