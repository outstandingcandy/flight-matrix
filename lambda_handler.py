"""
AWS Lambda handler for flight-matrix Flask application
Wraps Flask WSGI app using Mangum adapter for AWS Lambda compatibility
"""
import os
import sys
import logging

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure logging for Lambda
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('lambda_handler')

# Import Flask app
from web_app import app, init_app

# Initialize app on cold start (executed once per Lambda container)
init_app()
logger.info("Lambda handler initialized")

# Import Mangum adapter and WSGI-to-ASGI converter
from mangum import Mangum
from asgiref.wsgi import WsgiToAsgi

# Wrap Flask WSGI app in ASGI adapter, then wrap in Mangum for Lambda
asgi_app = WsgiToAsgi(app)
handler = Mangum(asgi_app, lifespan="off")

def lambda_handler(event, context):
    """
    AWS Lambda entry point

    Args:
        event: API Gateway event payload
        context: Lambda context object

    Returns:
        API Gateway response
    """
    try:
        # Log request details
        request_context = event.get('requestContext', {})
        http_context = request_context.get('http', {})
        method = http_context.get('method', 'UNKNOWN')
        path = http_context.get('path', '/')

        logger.info(f"Incoming request: {method} {path}")

        # Process request through Mangum adapter
        response = handler(event, context)

        logger.info(f"Response status: {response.get('statusCode', 'UNKNOWN')}")
        return response

    except Exception as e:
        logger.error(f"Lambda handler error: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': '{"error": "Internal server error"}'
        }
