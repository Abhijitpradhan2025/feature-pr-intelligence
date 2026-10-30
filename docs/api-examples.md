# API Examples

## Create customer

```bash
curl -X POST http://localhost:3000/api/v1/customers \
  -H "Content-Type: application/json" \
  -H "x-api-key: demo-order-api-key" \
  -d '{
    "firstName": "Rahul",
    "lastName": "Mehta",
    "email": "rahul@example.com",
    "phone": "+919876543210"
  }'
```

## Create order

```bash
curl -X POST http://localhost:3000/api/v1/orders \
  -H "Content-Type: application/json" \
  -H "x-api-key: demo-order-api-key" \
  -d '{
    "customerId": "CUSTOMER_ID",
    "currency": "INR",
    "shippingAddress": {
      "line1": "12 MG Road",
      "city": "Bengaluru",
      "state": "Karnataka",
      "postalCode": "560001",
      "country": "IN"
    },
    "items": [
      {
        "productId": "PRODUCT_ID",
        "quantity": 2
      }
    ],
    "couponCode": "WELCOME10"
  }'
```

## Record payment

```bash
curl -X POST http://localhost:3000/api/v1/payments \
  -H "Content-Type: application/json" \
  -H "x-api-key: demo-order-api-key" \
  -d '{
    "orderId": "ORDER_ID",
    "provider": "demo-gateway",
    "reference": "PAY-100001",
    "amountMinor": 100000,
    "currency": "INR"
  }'
```
