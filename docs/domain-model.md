# Domain Model

## Customer

A customer owns orders and one or more addresses.

## Product

A sellable catalogue item with a price, SKU, category and lifecycle status.

## Inventory

One inventory record per product. Available stock and reserved stock are tracked separately.

## Order

An order belongs to a customer and contains immutable pricing snapshots for each line item.

## Payment

A payment belongs to an order and has its own provider reference and lifecycle.

## AuditLog

Audit entries provide a simple event trail for operational traceability.

## Important invariant

`availableQty + reservedQty` represents the stock physically controlled by the platform, while the order lifecycle determines when reserved stock is released or consumed.
