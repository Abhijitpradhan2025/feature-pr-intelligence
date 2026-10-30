# Architecture

## Layering

The application follows a modular NestJS architecture.

```text
HTTP Controller
      |
      v
Application Service
      |
      +------ Audit Service
      |
      +------ Prisma Service
      |
      v
PostgreSQL
```

## Order creation

The order creation workflow is deliberately transaction-oriented.

```text
Validate request
      |
      v
Validate customer
      |
      v
Load products + inventory
      |
      v
Check stock
      |
      v
Calculate subtotal / discount / tax / shipping
      |
      v
Create order + line items
      |
      v
Reserve inventory
      |
      v
Write audit events
      |
      v
Commit transaction
```

If any step fails, the database transaction rolls back.

## Why integer money?

The API stores money as minor units.

Example:

```text
₹899.99 -> 89999 paise
```

This avoids common floating-point errors when multiplying prices and quantities.

## Extension points

The repository can be expanded with:

- Authentication and RBAC
- Payment provider adapters
- Kafka or event bus integration
- Email notifications
- Shipment management
- Returns and refunds
- Customer loyalty
- Product promotions
- Multi-warehouse inventory
- Frontend using React/Next.js
