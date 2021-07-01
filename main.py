#!/usr/bin/env python3
import argparse
import json
import logging
import os.path
import sys
from typing import List
from urllib.parse import urljoin

import yaml
from threescale_api import ThreeScaleClient

from config import Config, parse_config, ProductConfig, ApplicationConfig
from resources.application import Application, ApplicationPlan, ApplicationOIDCConfiguration
from resources.backend import Backend
from resources.metric import Metric
from resources.product import Product
from resources.proxy import Proxy, AuthenticationType, ProxyMapping

logger = logging.getLogger()
logger.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


def sync_mappings(client: ThreeScaleClient, product: Product, product_config: ProductConfig,
                  proxy_mappings: List[ProxyMapping]):
    # Delete extra mappings
    for mapping in ProxyMapping.list(client, product.id):
        should_be_active = True
        for active_mapping in proxy_mappings:
            if active_mapping.http_method == mapping.http_method and active_mapping.pattern == mapping.pattern:
                continue
            should_be_active = False
        if not should_be_active:
            mapping.delete(client, product.id)

    hits_metric = Metric.fetch_hits_metric(client, product.id)
    for mappingConfig in proxy_mappings:
        mappingConfig.metric_id = hits_metric.id  # set metric id on mapping (required)
        mappingConfig.pattern = product_config.api.publicBasePath + mappingConfig.pattern
        mapping = mappingConfig.create(client, product.id)


def sync(c: ThreeScaleClient, config: Config, open_api_basedir='.'):
    # TODO: Create user if not exists
    # Product variables
    environment = config.environment
    valid_methods = ['get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace']
    for product_config in config.products:
        product_name = product_config.name
        description = product_config.description
        version = product_config.version
        product_system_name = product_config.shortName.replace('-', '_').replace(' ', '_')

        # Parse OpenAPI spec for product.
        logger.info("Loading mapping paths from OpenAPI config.")
        with open(os.path.join(open_api_basedir, product_config.openAPIPath), 'r') as oas:
            if product_config.openAPIPath.endswith('.yml') or product_config.openAPIPath.endswith('.yaml'):
                openapi = yaml.load(oas.read(), Loader=yaml.FullLoader)
            elif product_config.openAPIPath.endswith('.json'):
                openapi = json.loads(oas.read())
            else:
                raise ValueError("Invalid file extension for OpenAPI spec, requires YAML or JSON. file={}".format(
                    product_config.openAPIPath))

        openapi_version: str = openapi['swagger'] if 'swagger' in openapi else openapi['openapi']
        api_base_path = '/'
        if openapi_version.startswith('2.') and 'basePath' in openapi:
            api_base_path = openapi['basePath']
        # TODO: OpenAPI 3.0 specifies basePath in the server object.

        proxy_mappings = []
        for path in openapi['paths']:
            definition = openapi['paths'][path]
            for method in [m for m in definition if m in valid_methods]:
                logger.info("Found mapping in spec: {} {}".format(method, urljoin(api_base_path, path[1:])))
                proxy_mappings.append(
                    ProxyMapping(http_method=method.upper(), pattern=urljoin(api_base_path, path[1:]) + '$', delta=1))

        # Create product
        product = Product(name=product_name, description=description, system_name=product_system_name)

        product = product.create(c)
        sync_applications(c, description, environment, product, product_config, product_system_name, version,
                          proxy_mappings)
        sync_mappings(client, product, product_config, proxy_mappings)
        # Promote application
        proxy = Proxy(service_id=product.id).fetch(c)
        proxy.promote(c)


def sync_applications(c: ThreeScaleClient, description: str, environment: str, product: Product,
                      product_config: ProductConfig, product_system_name: str, version: int,
                      proxy_mappings: List[ProxyMapping]):
    # Delete extra applications
    active_applications = [a.name for a in product_config.applications]
    for application in Application.list(client):
        if application.service_id == product.id and application.name not in active_applications:
            application.delete(client)
    for application_config in product_config.applications:
        user_id = fetch_user_id(c, application_config)

        # Generate names
        application_name = application_config.name \
            if application_config.name else f"{environment}_{product_system_name}_v{version}_Application"
        application_plan_name = f"{environment}_{product_system_name}_v{version}_AppPlan"
        backend_name = f"{environment}_{product_system_name}_backend"
        # Create application plans
        application_plan = ApplicationPlan(name=application_plan_name)
        application_plan = application_plan.create(c, service_id=product.id)
        # Create application
        logger.info("Creating application: {}".format(application_name))
        application = Application(name=application_name, client_id=application_config.client_id,
                                  client_secret=application_config.client_secret,
                                  description=description, account_id=user_id, plan_id=application_plan.id)
        application = application.create(c, delete_if_exists=True)
        # Configure authentication
        proxy = Proxy(service_id=product.id).fetch(c)
        proxy = proxy.update(c, oidc_issuer_endpoint=product_config.api.issuerURL,
                             oidc_issuer_type=product_config.api.issuerType,
                             credentials_location=product_config.api.credentialsLocation,
                             authentication_type=AuthenticationType.from_string(product_config.api.authType),
                             sandbox_endpoint=product_config.stagingPublicURL,
                             endpoint=product_config.productionPublicURL)
        sync_oidc_flows(c, product, product_config)
        sync_backends(c, backend_name, description, product, product_config)


def fetch_user_id(c: ThreeScaleClient, application_config: ApplicationConfig):
    # Verify user exists.
    users = [u.entity_id for u in c.accounts.list() if u.entity_name == application_config.account]
    if not users:
        raise ValueError('User {} not found.'.format(application_config.account))
    user_id = users[0]
    return user_id


def sync_backends(c: ThreeScaleClient, backend_name: str, description: str, product: Product,
                  product_config: ProductConfig):
    # Create backend
    for backend_config in product_config.backends:
        backend = Backend(name=backend_name, description=description,
                          private_endpoint=backend_config.privateBaseURL)
        backend = backend.create(c)
        # Update backend usages
        product.update_backends(c, backend_id=backend.id, path=backend_config.path)
        # backend.delete(c)


def sync_oidc_flows(c: ThreeScaleClient, product: Product, product_config: ProductConfig):
    oidc_config = ApplicationOIDCConfiguration.fetch(c, product.id)
    oidc_flows = product_config.api.oidcFlows
    oidc_config.direct_access_grants_enabled = \
        oidc_flows['directAccessGrants'] if 'directAccessGrants' in oidc_flows else False
    oidc_config.implicit_flow_enabled = oidc_flows['implicitFlow'] if 'implicitFlow' in oidc_flows else False
    oidc_config.service_accounts_enabled = oidc_flows['serviceAccounts'] if 'serviceAccounts' in oidc_flows else False
    oidc_config.standard_flow_enabled = oidc_flows['standardFlow'] if 'standardFlow' in oidc_flows else False
    oidc_config.update(c, product.id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync a 3scale API with OpenAPI mappings.')
    parser.add_argument('--3scale_url', dest='url', required=True, help='URL to the 3scale tenant admin.')
    parser.add_argument('--access_token', dest='token', required=True, help='Access token for the 3scale API.')
    parser.add_argument('--config', dest='config', required=False, default='config.yml', help='Path to config file.')
    parser.add_argument('--openapi_basedir', dest='openapi_basedir', required=False, default='.',
                        help='Directory root of OpenAPI specification files.')
    parser.add_argument('--delete', dest='delete', required=False, default=False, help='Delete all products.',
                        action='store_true')
    args = parser.parse_args()
    client = ThreeScaleClient(url=args.url, token=args.token, ssl_verify=True)

    with open(args.config, 'r') as f:
        loaded_config = yaml.load(f.read(), Loader=yaml.FullLoader)
        if not loaded_config:
            raise ValueError('Invalid config!')

    config = parse_config(loaded_config)

    if args.delete:
        response = input("WARNING --- Deleting all products in the configuration. Are you sure? y/N: ")
        if response.upper() == 'Y':
            logger.warning("Deleting {} products: {}".format(len(config.products), [p.name for p in config.products]))
            for p in config.products:
                system_name = p.shortName.replace('-', '_').replace(' ', '_')
                product = Product().fetch(client, system_name)
                if not product:
                    logger.error('Could not find product: {}, system_name={}'.format(p.name, system_name))
                    exit(1)
                product.delete(client)
    else:
        sync(client, config, open_api_basedir=args.openapi_basedir)
