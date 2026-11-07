import time
from datetime import datetime, timezone

import requests


PRIMARY = "http://127.0.0.1:5001"
REPLICA = "http://127.0.0.1:5002"
ORDERS = "http://127.0.0.1:5003"


def call(label, method, url, status=200, **kwargs):
    started = time.monotonic()
    response = requests.request(method, url, timeout=5, **kwargs)
    elapsed = time.monotonic() - started
    stamp = datetime.now(timezone.utc).isoformat()
    print(f"{stamp} {label}: HTTP {response.status_code} ({elapsed:.3f} s) {response.text.strip()}", flush=True)
    if response.status_code != status:
        raise RuntimeError(f"Expected HTTP {status} from {url}")
    return response.json()


def product_state(product):
    return product["price"], product["stock"], product["version"]


def check_state(primary, replica):
    for source, url, expected in (("Primary", PRIMARY, primary), ("Replica", REPLICA, replica)):
        product = call(source, "GET", f"{url}/products/1")
        assert product["id"] == 1 and product["name"] == "Laptop", product
        assert product_state(product) == expected, product


def check_order(source, expected):
    order = call(f"Order from {source}", "POST", f"{ORDERS}/orders", status=201, json={"product_id": 1, "quantity": 1, "catalog_source": source},)
    assert order["catalog_source"] == source and product_state(order) == expected, order


def update(mode, expected, **changes):
    result = call(f"{mode} PUT", "PUT", f"{PRIMARY}/products/1", json={"mode": mode, **changes})
    assert result["status"] == "SUCCESS" and product_state(result["product"]) == expected, result


def main():
    initial = (500000, 10, 1)
    updated_price = (450000, 10, 2)
    updated_stock = (450000, 5, 3)

    print("Experiment 1: asynchronous replication", flush=True)
    call("Primary health", "GET", f"{PRIMARY}/health")
    call("Replica health", "GET", f"{REPLICA}/health")
    call("Order health", "GET", f"{ORDERS}/health")
    print("Before the update", flush=True)
    check_state(initial, initial)
    check_order("replica", initial)
    update("async", updated_price, price=450000)
    print("Immediately after SUCCESS", flush=True)
    check_state(updated_price, initial)
    check_order("replica", initial)
    check_order("primary", updated_price)
    time.sleep(5)
    print("After waiting five seconds", flush=True)
    check_state(updated_price, updated_price)
    check_order("replica", updated_price)
    print("PASS: asynchronous replication\n", flush=True)

    print("Experiment 2: synchronous replication", flush=True)
    update("sync", updated_stock, stock=5)
    check_state(updated_stock, updated_stock)
    check_order("replica", updated_stock)
    print("PASS: synchronous replication", flush=True)


if __name__ == "__main__":
    main()
