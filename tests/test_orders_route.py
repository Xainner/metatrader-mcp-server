import sys, os
# Add src to path to import metatrader_openapi
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "src")))

import pytest
from fastapi.testclient import TestClient
from metatrader_openapi.main import app # Assuming 'app' is your FastAPI instance
# import metatrader_openapi.main as main_module # Not strictly needed if 'app' is directly imported
from metatrader_client.client import MT5Client # To help with mocking
from unittest.mock import MagicMock, patch

# Mock for the MT5Client instance and its methods
@pytest.fixture
def mock_mt5_client_order_methods(openapi_client): # Renamed fixture for clarity
    mock_place_market_order = MagicMock(return_value={
        "error": False,
        "message": "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 12345)",
        "data": MagicMock(
            request=MagicMock(symbol="EURUSD", sl=1.0990, tp=1.1010, comment="custom_comment_test"), # Added comment for testing
            volume=0.01,
            price=1.1000,
            order=12345
        )
    })
    openapi_client.order.place_market_order = mock_place_market_order
    return mock_place_market_order # Return the mock that will be asserted

def test_place_market_order_api_with_sl_tp(mock_mt5_client_order_methods): # Inject the patched mock
    with TestClient(app) as api_client:
        order_payload = {
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "BUY",
            "stop_loss": 1.0990,
            "take_profit": 1.1010
            # "comment": "custom_comment_test" # Not yet part of the task, but good for future
        }
        # Ensure endpoint is correct, e.g. /api/v1/order/market
        # Based on typical FastAPI structure and if orders_router is prefixed with /api/v1/orders
        response = api_client.post("/api/v1/order/market", json=order_payload)

    assert response.status_code == 200, response.text
    response_json = response.json()
    assert response_json["error"] is False
    # Check message carefully, it might vary based on actual implementation
    assert "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 12345)" in response_json["message"]
    
    # Assert that the underlying client method was called correctly by the API route
    mock_mt5_client_order_methods.assert_called_once_with(
        symbol="EURUSD",
        volume=0.01,
        type="BUY",
        stop_loss=1.0990,
        take_profit=1.1010,
        request_id=None, strategy_id=None, preview_id=None,
    )

def test_place_market_order_api_no_sl_tp(mock_mt5_client_order_methods): # Test without SL/TP
    mock_mt5_client_order_methods.reset_mock() # Reset mock for this new test case
    
    # Adjust return value for this specific test case if needed
    mock_mt5_client_order_methods.return_value = {
        "error": False,
        "message": "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 67890) (no SL/TP)",
        "data": MagicMock(
            request=MagicMock(symbol="EURUSD", sl=0.0, tp=0.0, comment=None), 
            volume=0.01,
            price=1.1000,
            order=67890
        )
    }

    with TestClient(app) as api_client:
        order_payload = {
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "BUY"
            # No stop_loss or take_profit here, relying on defaults in the endpoint
        }
        response = api_client.post("/api/v1/order/market", json=order_payload)

    assert response.status_code == 200, response.text
    response_json = response.json()
    assert response_json["error"] is False
    assert "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 67890) (no SL/TP)" in response_json["message"]
    
    mock_mt5_client_order_methods.assert_called_once_with(
        symbol="EURUSD",
        volume=0.01,
        type="BUY",
        stop_loss=0.0, # Default SL value expected by place_market_order in routers/orders.py
        take_profit=0.0,  # Default TP value
        request_id=None, strategy_id=None, preview_id=None,
    )

# Example for a test case with invalid input
def test_place_market_order_api_invalid_type(mock_mt5_client_order_methods):
    mock_mt5_client_order_methods.reset_mock()
    # The client method might not even be called if validation fails at FastAPI level
    # However, if the call to the MT5 client happens and it returns an error:
    mock_mt5_client_order_methods.return_value = {
        "error": True,
        "message": "Invalid type, should be BUY or SELL.", # This message comes from place_market_order client method
        "data": None
    }

    with TestClient(app) as api_client:
        order_payload = {
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "INVALID_TYPE", # Invalid order type
            "stop_loss": 1.0990,
            "take_profit": 1.1010
        }
        response = api_client.post("/api/v1/order/market", json=order_payload)

    # Depending on where validation happens (FastAPI or client method), status code might differ
    # If FastAPI handles it via Enum in Pydantic model, it would be 422
    # If the error comes from the client method as mocked, it could be 200 with error:true or a mapped HTTP error
    # The current place_market_order in routers/orders.py calls client.order.place_market_order
    # and then returns its result. If that result has error:true, it's passed on.
    # The client's place_market_order has its own "Invalid type" check.

    assert response.status_code == 200 # Assuming the route itself doesn't throw HTTPException for this
    response_json = response.json()
    assert response_json["error"] is True
    assert "Invalid type, should be BUY or SELL." in response_json["message"]

    # Check if the mock was called with the invalid type, or not called if validation is earlier
    # Based on current setup, place_market_order in the client is called.
    mock_mt5_client_order_methods.assert_called_once_with(
        symbol="EURUSD",
        volume=0.01,
        type="INVALID_TYPE",
        stop_loss=1.0990,
        take_profit=1.1010,
        request_id=None, strategy_id=None, preview_id=None,
    )

# Add more tests: e.g., missing required fields (FastAPI should catch this with 422)
def test_place_market_order_api_missing_fields(openapi_client): # No mock needed if FastAPI validation catches it
    with TestClient(app) as api_client:
        order_payload = {
            "symbol": "EURUSD",
            # volume and type are missing
        }
        response = api_client.post("/api/v1/order/market", json=order_payload)
    assert response.status_code == 422 # FastAPI's Unprocessable Entity for validation errors
    # Optionally, check the content of response.json()["detail"] for specifics
    response_json = response.json()
    assert any(field['msg'].lower() == 'field required' and field['loc'] == ['body', 'volume'] for field in response_json['detail'])
    assert any(field['msg'].lower() == 'field required' and field['loc'] == ['body', 'type'] for field in response_json['detail'])

# Reminder: Ensure that the FastAPI app (`app`) and the router prefix are correctly configured
# for the endpoint `/api/v1/order/market`.
# If `main.py` includes `orders.router` with `prefix="/api/v1/orders"`,
# and `orders.router` has a POST route at `/market`, then `/api/v1/order/market` is correct.

# To confirm router prefix, I might need to read 'src/metatrader_openapi/main.py'
# For now, proceeding with the assumption.
