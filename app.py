#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import aws_cdk as cdk

from infra.stack import MicrovmSessionsStack

app = cdk.App()
MicrovmSessionsStack(app, "MicrovmSessions")
app.synth()
