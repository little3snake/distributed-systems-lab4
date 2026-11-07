import argparse
import math
from datetime import datetime, timezone
import threading

from flask import Flask, jsonify, request
import requests
app = Flask(__name__)

orders = {}
next_order_id = 1
orders_lock = threading.Lock()

catalog_urls = {"primary": "http://127.0.0.1:5001", "replica": "http://127.0.0.1:5002"}
catalog_timeout = 2.0

def utc_now():
    # return an ISO 8601 timestamp with an explicit UTC offset
    return datetime.now(timezone.utc).isoformat()

@app.errorhandler(404)
def handle_route_not_found(_error):
    return jsonify({"error": "Route not found"}), 404

@app.errorhandler(405)
def handle_method_not_allowed(_error):
    return jsonify({"error": "Method not allowed"}), 405

@app.get("/health")
def health():
    return jsonify({"service": "orders", "status": "ok"}), 200

def fetch_product(product_id, catalog_source):
    url = f"{catalog_urls[catalog_source]}/products/{product_id}"
    try:
        response = requests.get(url, timeout=catalog_timeout)
    except requests.Timeout:
        return None, (
            jsonify(
                {
                    "error": "Catalog service is unavailable",
                    "reason": "Request timed out",
                }
            ),
            503,
        )
    except requests.ConnectionError:
        return None, (
            jsonify(
                {
                    "error": "Catalog service is unavailable",
                    "reason": "Connection failed",
                }
            ),
            503,
        )
    except requests.RequestException:
        return None, (jsonify({"error": "Catalog service request failed"}), 503,)

    if response.status_code == 404:
        return None, (jsonify({"error": "Product not found in Catalog"}), 404)
    if response.status_code != 200:
        return None, (jsonify({"error": "Catalog service returned an unexpected response"}), 502,)

    try:
        return response.json(), None
    except ValueError:
        return None, (jsonify({"error": "Catalog service returned invalid JSON"}), 502,)

@app.post("/orders")
def create_order():
    global next_order_id
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    product_id = data.get("product_id")
    quantity = data.get("quantity")
    catalog_source = data.get("catalog_source", request.args.get("catalog_source", "primary"))
    if catalog_source not in ("primary", "replica"):
        return jsonify({"error": "Field 'catalog_source' must be primary or replica"}), 400
    if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id < 1:
        return jsonify({"error": "Field 'product_id' must be a positive integer"}), 400
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        return jsonify({"error": "Field 'quantity' must be a positive integer"}), 400

    # do not hold orders_lock during the network request
    # other request threads remain free to read saved orders while this thread waits for catalog
    product, error_response = fetch_product(product_id, catalog_source)
    if error_response is not None:
        return error_response

    total = round(product["price"] * quantity, 2)
    with orders_lock:
        order = {
            "id": next_order_id,
            "product_id": product["id"],
            "name": product["name"],
            "price": product["price"],
            "stock": product["stock"],
            "version": product["version"],
            "catalog_source": catalog_source,
            "quantity": quantity,
            "total": total,
            "created_at": utc_now(),
        }
        orders[next_order_id] = order
        next_order_id += 1

    return jsonify(order), 201

@app.get("/orders/<int:order_id>")
def get_order(order_id):
    with orders_lock:
        order = orders.get(order_id)

    if order is None:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(order), 200

def parse_arguments():
    parser = argparse.ArgumentParser(description="Lab 4 Orders service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--catalog-url", default=catalog_urls["primary"])
    parser.add_argument("--replica-url", default=catalog_urls["replica"])
    parser.add_argument("--catalog-timeout", type=float, default=2.0)
    return parser.parse_args()

def main():
    global catalog_timeout

    args = parse_arguments()
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port must be between 1 and 65535")
    if not math.isfinite(args.catalog_timeout) or args.catalog_timeout <= 0:
        raise SystemExit("Catalog timeout must be finite and greater than zero")

    catalog_urls.update(primary=args.catalog_url.rstrip("/"), replica=args.replica_url.rstrip("/"))
    catalog_timeout = args.catalog_timeout
    print(f"Catalog endpoints: {catalog_urls}; timeout: {catalog_timeout:.2f}s", flush=True)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
