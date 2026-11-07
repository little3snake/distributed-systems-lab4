import argparse
import math
from datetime import datetime, timezone
import threading

from flask import Flask, jsonify, request
import requests

app = Flask(__name__)
app.config.update(ROLE="primary", REPLICA_URL="http://127.0.0.1:5002")

products = {1: {"id": 1, "name": "Laptop", "price": 500000, "stock": 10, "version": 1}}
next_product_id = 2
products_lock = threading.Lock()
write_lock = threading.Lock()
replication_delay = 5


def utc_now():
    # return an ISO 8601 timestamp with an explicit UTC offset
    return datetime.now(timezone.utc).isoformat()

def normalize_price(value):
    # validate a positive price and keep whole-number prices as integers
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0: return None
    if isinstance(value, float) and not math.isfinite(value): return None
    normalized = value if isinstance(value, int) else round(value, 2)
    return normalized if normalized > 0 else None

@app.errorhandler(404)
def handle_route_not_found(_error):
    return jsonify({"error": "Route not found"}), 404

@app.errorhandler(405)
def handle_method_not_allowed(_error):
    return jsonify({"error": "Method not allowed"}), 405

@app.get("/health")
def health():
    return jsonify({"service": "catalog", "role": app.config["ROLE"], "status": "ok"}), 200


def log(message):
    print(f"{utc_now()} [{app.config['ROLE']}] {message}", flush=True)


def replicate(product):
    version = product["version"]
    try:
        response = requests.put(
            f"{app.config['REPLICA_URL']}/replication/products/{product['id']}",
            json=product,
            timeout=2,
        )
        response.raise_for_status()
        result = response.json()
        if result == {"status": "ACK", "version": version}:
            log(f"received ACK for product {product['id']} v{version}")
            return True
        if isinstance(result, dict) and result.get("status") == "SUPERSEDED":
            log(f"product {product['id']} v{version} already superseded on replica")
            return False
    except (requests.RequestException, ValueError) as error:
        log(f"replication request failed: {error}")
    log(f"missing ACK for product {product['id']} v{version}")
    return False


@app.put("/replication/products/<int:product_id>")
def apply_replication(product_id):
    if app.config["ROLE"] != "replica":
        return jsonify({"error": "Replication endpoint is available on the replica"}), 405

    product = request.get_json(silent=True)
    if (
        not isinstance(product, dict)
        or type(product.get("id")) is not int
        or product["id"] != product_id
        or type(product.get("version")) is not int
        or product["version"] < 1
        or not isinstance(product.get("name"), str)
        or not product["name"].strip()
        or normalize_price(product.get("price")) is None
        or type(product.get("stock")) is not int
        or product["stock"] < 0
    ):
        return jsonify({"error": "Invalid product snapshot"}), 400

    version = product["version"]
    with products_lock:
        current = products.get(product_id)
        if current is not None and current["version"] > version:
            log(f"ignored older product {product_id} v{version}, current v{current['version']}")
            return jsonify({"status": "SUPERSEDED", "version": current["version"]}), 200
        if current is not None and current["version"] == version and current != product:
            return jsonify({"error": "Conflicting product snapshot for this version"}), 409
        products[product_id] = product
        log(f"applied product {product_id} v{version}, returning ACK")
    return jsonify({"status": "ACK", "version": version}), 200


@app.put("/products/<int:product_id>")
def update_product(product_id):
    if app.config["ROLE"] != "primary":
        return jsonify({"error": "Send product updates to the primary"}), 405

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    mode = data.get("mode")
    if mode not in ("async", "sync"):
        return jsonify({"error": "Field 'mode' must be async or sync"}), 400

    changes = {}
    if "name" in data:
        if not isinstance(data["name"], str) or not data["name"].strip():
            return jsonify({"error": "Field 'name' must be a non-empty string"}), 400
        changes["name"] = data["name"].strip()
    if "price" in data:
        price = normalize_price(data["price"])
        if price is None:
            return jsonify({"error": "Field 'price' must be a positive number"}), 400
        changes["price"] = price
    if "stock" in data:
        stock = data["stock"]
        if type(stock) is not int or stock < 0:
            return jsonify({"error": "Field 'stock' must be a non-negative integer"}), 400
        changes["stock"] = stock
    if not changes:
        return jsonify({"error": "Provide at least one of name, price or stock"}), 400

    with write_lock:
        with products_lock:
            current = products.get(product_id)
            if current is None:
                return jsonify({"error": "Product not found"}), 404
            product = {**current, **changes, "version": current["version"] + 1}
            products[product_id] = product

        version = product["version"]
        log(f"stored product {product_id} v{version}, mode={mode}")
        if mode == "async":
            delivery = threading.Timer(replication_delay, replicate, args=(product,))
            delivery.daemon = True
            delivery.start()
        elif not replicate(product):
            return jsonify({"error": "Replica did not acknowledge update", "product": product}), 503
        log(f"returning SUCCESS for product {product_id} v{version}, mode={mode}")
        return jsonify({"status": "SUCCESS", "product": product}), 200

@app.post("/products")
def create_product():
    global next_product_id

    if app.config["ROLE"] != "primary":
        return jsonify({"error": "Send product creation requests to the primary"}), 405
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name = data.get("name")
    price = normalize_price(data.get("price"))
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Field 'name' must be a non-empty string"}), 400
    if price is None:
        return jsonify({"error": "Field 'price' must be a positive number"}), 400
    stock = data.get("stock", 0)
    if type(stock) is not int or stock < 0:
        return jsonify({"error": "Field 'stock' must be a non-negative integer"}), 400

    # id generation and dictionary update form one critical section
    # only one request thread may execute it at a time.
    with products_lock:
        product = {
            "id": next_product_id,
            "name": name.strip(),
            "price": price,
            "stock": stock,
            "version": 1,
            "created_at": utc_now(),
        }
        products[next_product_id] = product
        next_product_id += 1
    replicate(product)
    return jsonify(product), 201

@app.get("/products/<int:product_id>")
def get_product(product_id):
    with products_lock:
        product = products.get(product_id)
    if product is None:
        return jsonify({"error": "Product not found"}), 404
    return jsonify(product), 200

@app.get("/products")
def get_products():
    with products_lock:
        result = list(products.values())
    return jsonify(result), 200

def parse_arguments():
    parser = argparse.ArgumentParser(description="Lab 4 Catalog service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--role", choices=("primary", "replica"), default="primary")
    parser.add_argument("--port", type=int)
    parser.add_argument("--replica-url", default=app.config["REPLICA_URL"])
    return parser.parse_args()

def main():
    args = parse_arguments()
    port = args.port if args.port is not None else {"primary": 5001, "replica": 5002}[args.role]
    if not 1 <= port <= 65535:
        raise SystemExit("Port must be between 1 and 65535")
    app.config.update(ROLE=args.role, REPLICA_URL=args.replica_url.rstrip("/"))
    # threaded=True lets Flask handle independent requests concurrently.
    app.run(host=args.host, port=port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
