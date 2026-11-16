// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// CloudFront Function (viewer request) for /ide/* on the IDE distribution.
//
// The IDE has its own hostname, separate from the start page, so code running
// inside a MicroVM can't reach the start page's Cognito tokens.
//
// Browsers cannot attach the x-aws-proxy-auth header that every MicroVM request
// needs, so CloudFront does it for them. The /launch endpoint set two HttpOnly
// cookies for the signed-in user: their MicroVM endpoint and a short-lived
// MicroVM auth token. For every request (page loads, assets, and the VS Code
// WebSocket) this function points the request at that user's MicroVM and adds
// the token as an origin header. The cookies are stripped before forwarding.
import cf from 'cloudfront';

// Only MicroVM endpoints in this Region may be used as an origin. This stops a
// forged cookie from turning CloudFront into a proxy for arbitrary hosts.
const ENDPOINT = /^[a-z0-9-]+\.lambda-microvm\.__REGION__\.on\.aws$/;

// No or expired session: back to the start page, which renews it.
const EXPIRED = '__SITE_URL__/?session=expired';

function cookie(request, name) {
  const c = request.cookies[name];
  return c ? c.value : undefined;
}

function handler(event) {
  const request = event.request;
  // Only /ide is VS Code; nothing else on this host reaches a MicroVM.
  if (request.uri !== '/ide' && request.uri.indexOf('/ide/') !== 0) {
    return {
      statusCode: 302,
      statusDescription: 'Found',
      headers: { location: { value: '__SITE_URL__/' }, 'cache-control': { value: 'no-store' } },
    };
  }
  const endpoint = cookie(request, 'mvm_ep');
  const token = cookie(request, 'mvm_tok');

  if (!endpoint || !token || !ENDPOINT.test(endpoint)) {
    return {
      statusCode: 302,
      statusDescription: 'Found',
      headers: { location: { value: EXPIRED }, 'cache-control': { value: 'no-store' } },
    };
  }

  delete request.cookies['mvm_ep'];
  delete request.cookies['mvm_tok'];
  // The proxy header namespace is reserved; never pass viewer-supplied values on.
  Object.keys(request.headers).forEach(function (name) {
    if (name.indexOf('x-aws-proxy-') === 0) delete request.headers[name];
  });

  cf.updateRequestOrigin({
    domainName: endpoint,
    customHeaders: { 'x-aws-proxy-auth': token },
  });
  return request;
}
