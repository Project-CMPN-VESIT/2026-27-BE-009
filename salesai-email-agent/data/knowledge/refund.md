# ShopiFyX Refund Policy

## 1. Refund Eligibility & Request Window
Customers may request a refund within **7 calendar days** of the confirmed product delivery date. Refund requests initiated after the 7-day delivery window are ineligible for return or refund, except in cases of verified manufacturer warranty claims.

## 1A. SKU Refund Allowance (Mandatory Payout Cap)

SalesAI and the ShopiFyX refund-agent must never refund the original order basket total.
A refund is allowed only for the returned SKU, and only up to that SKU's published **Refund Allowance**.

Extraction rules for the refund-agent:
1. Identify the returned SKU from the customer issue and the order line items in PostgreSQL.
2. Read RAG policy documents and extract `**Refund Allowance:** ₹X` for that SKU from `product_catalog.md`, or this table.
3. If that rupee line is missing, fall back to `products.refund_allowance` from PostgreSQL (same published cap).
4. Refund `min(extracted Refund Allowance × quantity, amount paid for that SKU)`.
5. The policy deduction is `(amount paid for the SKU − extracted Refund Allowance)`. Do not invent a percentage in code.
6. Do not add shipping, other items, or the order `total_amount` unless those SKUs are also being returned.
7. If no rupee Refund Allowance can be extracted, do not generate a refund.
8. Never fall back to `**Price:**`. Price is what the customer paid, not what ShopiFyX allows back.

How ShopiFyX currently publishes those rupee caps in this knowledge base:
- **Processing deduction rate:** 10% of listed SKU price
- **Refund Allowance** = listed price minus that published deduction

That rate is for catalog authors. The refund-agent must still pay the extracted rupee **Refund Allowance**, not recompute 10% at runtime. If a SKU line is edited, the next refund follows the new rupee amount.

Never pay the original item price or the order basket total. Published SKU Refund Allowance table:

| SKU | Product | Refund Allowance (INR) |
|---|---|---|
| AL-001 | TravelPro Cabin Backpack 28L | ₹2,699.10 |
| AL-002 | TravelPro Laptop Backpack 20L | ₹1,979.10 |
| AL-003 | TravelPro Hard Shell Cabin Case | ₹4,499.10 |
| AL-004 | TravelPro Packing Cube Set | ₹1,169.10 |
| AL-005 | CarryLite Everyday Tote | ₹899.10 |
| AL-006 | CarryLite Sling Bag | ₹1,079.10 |
| AL-007 | CarryLite RFID Wallet | ₹809.10 |
| AL-008 | FitLife Stainless Steel Bottle 750ml | ₹1,349.10 |
| AL-009 | FitLife Sports Bottle 1L | ₹899.10 |
| AL-010 | FitLife Yoga Mat Pro | ₹1,619.10 |
| AL-011 | FitLife Resistance Band Set | ₹1,169.10 |
| AL-012 | DeskMate Adjustable Laptop Stand | ₹1,799.10 |
| AL-013 | DeskMate Desk Organizer | ₹719.10 |
| AL-014 | DeskMate USB Desk Fan | ₹809.10 |
| AL-015 | FitLife Massage Gun Mini | ₹3,149.10 |
| ELX-001 | ShopiFyX AirBeat Pro Earbuds | ₹3,599.10 |
| ELX-002 | ShopiFyX SoundMax Bluetooth Speaker | ₹2,699.10 |
| ELX-003 | NovaTech Pulse Smartwatch | ₹4,949.10 |
| ELX-004 | NovaTech FitBand 3 | ₹1,799.10 |
| ELX-005 | PixelCore Wireless Keyboard | ₹2,249.10 |
| ELX-006 | PixelCore Precision Mouse | ₹1,349.10 |
| ELX-007 | VoltEdge 65W GaN Charger | ₹2,069.10 |
| ELX-008 | VoltEdge 10000mAh Power Bank | ₹1,619.10 |
| ELX-009 | ViewMax 27-inch QHD Monitor | ₹17,099.10 |
| ELX-010 | ViewMax Full HD Webcam | ₹3,149.10 |
| ELX-011 | EchoTune Studio Headphones | ₹6,299.10 |
| ELX-012 | EchoTune Mini Buds | ₹2,249.10 |
| ELX-013 | HomeLink Smart Plug | ₹899.10 |
| ELX-014 | HomeLink Smart LED Strip | ₹1,439.10 |
| ELX-015 | DriveSafe Dash Camera | ₹4,049.10 |
| FW-001 | UrbanStep Flex Running Shoes | ₹2,699.10 |
| FW-002 | UrbanStep TrailGrip Shoes | ₹3,599.10 |
| FW-003 | UrbanStep Everyday Sneakers | ₹2,249.10 |
| FW-004 | ModeCraft Classic Denim Jacket | ₹2,519.10 |
| FW-005 | ModeCraft Oversized Hoodie | ₹1,709.10 |
| FW-006 | ModeCraft Essential Cotton T-Shirt | ₹719.10 |
| FW-007 | ModeCraft Performance Polo | ₹1,169.10 |
| FW-008 | StreetLine Cargo Trousers | ₹1,799.10 |
| FW-009 | StreetLine Slim Chinos | ₹1,619.10 |
| FW-010 | StreetLine Everyday Joggers | ₹1,349.10 |
| FW-011 | UrbanStep Comfort Sandals | ₹1,259.10 |
| FW-012 | ModeCraft Linen Summer Shirt | ₹1,979.10 |
| FW-013 | StreetLine Classic Bomber Jacket | ₹2,969.10 |
| FW-014 | UrbanStep Knit Walking Shoes | ₹2,429.10 |
| FW-015 | ModeCraft Winter Puffer Jacket | ₹4,499.10 |
| HK-001 | KitchenPro Air Fryer 4L | ₹4,499.10 |
| HK-002 | KitchenPro Air Fryer 6L | ₹6,299.10 |
| HK-003 | KitchenPro Electric Kettle 1.5L | ₹1,439.10 |
| HK-004 | KitchenPro Mixer Grinder 750W | ₹2,969.10 |
| HK-005 | BrewMaster Coffee Maker | ₹2,699.10 |
| HK-006 | BrewMaster Milk Frother | ₹1,619.10 |
| HK-007 | CleanHome Cordless Vacuum | ₹8,099.10 |
| HK-008 | CleanHome Robot Vacuum Mini | ₹14,399.10 |
| HK-009 | HomeNest Smart Table Lamp | ₹1,979.10 |
| HK-010 | HomeNest Memory Foam Pillow | ₹1,169.10 |
| HK-011 | HomeNest Weighted Blanket | ₹3,149.10 |
| HK-012 | KitchenPro Non-Stick Cookware Set | ₹4,049.10 |
| HK-013 | BrewMaster Electric Toaster | ₹1,709.10 |
| HK-014 | CleanHome Portable Steam Cleaner | ₹2,699.10 |
| HK-015 | HomeNest Digital Kitchen Scale | ₹809.10 |

## 2. Mandatory Return & Refund Conditions
To qualify for an approved refund, the returned merchandise must satisfy the following criteria:
- **Unused & Unworn**: The item must be in its original, pristine condition with no signs of wear, stains, wash cycles, perfume odors, or alterations.
- **Original Packaging & Tags**: All original brand tags, price tags, barcode stickers, user manuals, accessories, dust bags, and original retail packaging must be returned intact.
- **Invoice / Proof of Purchase**: The customer must provide the digital order ID or physical invoice included in the package.
- **No Customer Misuse**: Products showing physical damage, water intrusion, drop damage, or improper handling by the customer are not eligible for a refund.

---

## 3. End-to-End Refund Process & Stages

The standard ShopiFyX refund lifecycle follows a 5-step verification process:
1. **Refund Request Submission**: The customer initiates a return/refund request via the ShopiFyX app, website, or customer support email with the Order ID and reason for return.
2. **Review & Verification**: ShopiFyX support reviews the request within 24 hours and issues a Reverse Pickup tracking number.
3. **Doorstep Reverse Pickup**: A courier agent arrives at the delivery address within **2 to 4 business days** to collect the item.
4. **Warehouse Quality Inspection (QC)**: Once the returned item reaches the fulfillment center, our quality assurance team inspects the item within **24 to 48 hours**.
5. **Refund Initiation & Gateway Credit**: Upon successful QC approval, the refund is initiated immediately to the customer's payment destination.

---

## 4. Refund Credit Timelines by Payment Method

The actual turnaround time for funds to reflect in the customer's account depends strictly on the original payment method:

| Payment Method | Refund Processing Timeline | Destination Account |
|---|---|---|
| **UPI (Google Pay, PhonePe, Paytm, BHIM)** | **24 to 48 business hours** | Original Bank Account linked to UPI ID |
| **ShopiFyX Wallet / Digital Gift Card** | **Instant to 24 hours** | ShopiFyX Wallet Balance |
| **Net Banking (Direct Bank Transfer)** | **3 to 5 business days** | Source Bank Account |
| **Credit Card (Visa, Mastercard, RuPay, Amex)** | **5 to 7 business days** | Card Statement / Available Credit Limit |
| **Debit Card** | **5 to 7 business days** | Bank Account linked to Debit Card |
| **Cash on Delivery (COD)** | **24 to 48 hours** (after bank details submitted) | Direct NEFT Bank Transfer or ShopiFyX Wallet |

*Note*: Timelines reflect banking business days (excluding bank holidays and weekends).

---

## 5. Cash on Delivery (COD) Refund Procedure
For orders originally paid via Cash on Delivery:
- Once the returned product passes quality inspection, ShopiFyX sends a secure, encrypted SMS/Email link where the customer enters their verified Bank Account Number and IFSC code, or opts for instant ShopiFyX Wallet credit.
- The funds are transferred via IMPS/NEFT within **24 to 48 hours** after bank details are submitted.

---

## 6. Non-Refundable Items & Categories
For health, hygiene, and digital rights reasons, the following categories are strictly non-refundable and non-returnable:
- Personal care, cosmetics, grooming products, and fragrances.
- Opened or unsealed electronics, earphones, and wearables (unless dead on arrival/defective).
- Innerwear, lingerie, swimwear, and socks.
- Clearance sale / Flash sale items marked *"Final Sale"*.
- Digital Gift Cards and downloadable software vouchers.

---

## 7. Partial Refunds and Bundle Adjustments
- If a customer purchased items under a combo offer (e.g., *"Buy 3 for ₹999"*) and returns only 1 item, the refund is calculated from that SKU's published Refund Allowance, never the original order `total_amount`.
- Original outbound shipping charges (₹49 for orders below ₹999) are non-refundable unless the return is due to a ShopiFyX fulfillment defect or damaged delivery.
- The refund-agent must extract the SKU Refund Allowance from this policy / product catalog before generating any payout.

---

## 8. AI & Support Policy Guardrails
- **No Speculative Instant Refund Promises**: SalesAI must never promise that a refund has already been completed or will arrive today without verified transaction status.
- **Accurate Published Ranges**: SalesAI must inform customers of the standard 5–7 business days window (or 24–48h for UPI) and explain the 5-stage return inspection process clearly.