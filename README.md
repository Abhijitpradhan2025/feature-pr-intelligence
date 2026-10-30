# Customer Order Platform

A production-style TypeScript/NestJS backend for creating and managing customer orders.

The repository is intentionally structured like a larger enterprise application, with separate modules for customers, products, inventory, orders, payments, audit history, shared infrastructure, Prisma, unit tests, and end-to-end tests.

## What this application does

- Create and manage customers
- Maintain a product catalogue with categories and pricing
- Track inventory and prevent overselling
- Build an order from multiple line items
- Calculate subtotal, discount, tax, shipping, and grand total
- Reserve inventory atomically when an order is created
- Move orders through a controlled lifecycle
- Record payments and payment status
- Record an audit trail for important order changes
- Provide dashboard and reporting endpoints
- Validate incoming API payloads
- Expose health and readiness endpoints
- Run locally with PostgreSQL through Docker Compose

## Architecture

```text
src/
├── app/
│   ├── audit/
│   ├── customers/
│   ├── inventory/
│   ├── orders/
│   ├── payments/
│   ├── products/
│   └── reporting/
├── assets/
│   └── images/
├── common/
│   ├── decorators/
│   ├── filters/
│   ├── guards/
│   ├── interceptors/
│   ├── pipes/
│   └── utils/
├── prisma/
│   ├── prisma.service.ts
│   └── prisma.module.ts
├── tests/
│   ├── fixtures/
│   └── helpers/
├── app.module.ts
└── main.ts
prisma/
├── migrations/
├── schema.prisma
└── seed.ts
e2e/
└── orders.e2e-spec.ts
```

## Main API endpoints

| Area | Endpoint | Purpose |
|---|---|---|
| Health | `GET /api/v1/health` | Liveness |
| Customers | `POST /api/v1/customers` | Create a customer |
| Customers | `GET /api/v1/customers` | Search customers |
| Products | `POST /api/v1/products` | Create a product |
| Products | `GET /api/v1/products` | Search catalogue |
| Inventory | `POST /api/v1/inventory/adjust` | Adjust stock |
| Orders | `POST /api/v1/orders` | Create an order |
| Orders | `GET /api/v1/orders/:id` | Get order details |
| Orders | `GET /api/v1/orders` | Search orders |
| Orders | `PATCH /api/v1/orders/:id/status` | Change order status |
| Payments | `POST /api/v1/payments` | Record a payment |
| Audit | `GET /api/v1/audit/orders/:orderId` | Order audit history |
| Reporting | `GET /api/v1/reporting/dashboard` | Operational metrics |

## Quick start

### 1. Install

```bash
npm install
```

### 2. Start PostgreSQL

```bash
docker compose up -d postgres
```

### 3. Configure environment

```bash
copy .env.example .env
```

On macOS/Linux:

```bash
cp .env.example .env
```

### 4. Create the database schema and seed data

```bash
npm run prisma:generate
npm run prisma:migrate
npm run prisma:seed
```

### 5. Run the API

```bash
npm run start:dev
```

The API will be available at:

```text
http://localhost:3000/api/v1
```

## Example order request

```json
{
  "customerId": "replace-with-customer-id",
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
      "productId": "replace-with-product-id",
      "quantity": 2
    }
  ],
  "couponCode": "WELCOME10"
}
```

## Example order lifecycle

```text
DRAFT
  ↓
PENDING_PAYMENT
  ↓
CONFIRMED
  ↓
PROCESSING
  ↓
SHIPPED
  ↓
DELIVERED
```

Cancellation is allowed from the pre-shipment states.

## Design notes

The order creation path uses a database transaction so that inventory reservation, order creation, line-item creation, and audit creation succeed or fail together.

Money is stored as integer minor units, such as paise for INR, to avoid floating-point rounding issues.

The code is intentionally more extensive than a simple CRUD starter so it can be used as a realistic GitHub repository for demonstrations, architecture reviews, code analysis, or interview exercises.

## Useful commands

```bash
npm run start:dev
npm run build
npm run lint
npm run test
npm run test:watch
npm run test:e2e
npm run prisma:studio
```

## License

MIT
