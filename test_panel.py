import asyncio
import pytest
from panel_api import PanelAPI


@pytest.mark.asyncio
async def test_panel():
    api = PanelAPI()
    try:
        balance = await api.get_balance()
        print("Panel Balance:", balance)

        services = await api._post("services")
        if isinstance(services, list) and len(services) > 0:
            print(f"Found {len(services)} services.")
            # Let's print the first 5 services to find a cheap one
            for s in services[:5]:
                print(s)

            # Find the cheapest service
            cheap_service = min(services, key=lambda x: float(x.get("rate", 999)))
            print("Cheapest service:", cheap_service)

            # Try to place a minimal order
            try:
                min_q = int(cheap_service.get("min", 10))
                order_id = await api.add_order(
                    service_id=int(cheap_service["service"]),
                    link="https://example.com",
                    quantity=min_q,
                )
                print(f"Order successful! Order ID: {order_id}")
            except Exception as e:
                print(f"Order failed: {e}")

    except Exception as e:
        print("API Error:", e)
    finally:
        await api.aclose()


asyncio.run(test_mtp())
