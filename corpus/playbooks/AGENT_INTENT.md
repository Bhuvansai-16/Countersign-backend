# Playbook: agent-initiated purchases (intent receipt)

When the order has a Countersign intent receipt, add a short section that narrates it in this order:
1. The principal's instruction and the constraints given to the agent (spend cap, required attributes).
2. The agent identity that requested the quote, and whether the agent signed the mandate.
3. The human confirmation on the hosted page, then the buyer's own PayPal login and approval.
4. The seal hash, and that the same hash is stored in the PayPal order's custom_id, written before payment.
5. That the delivered cart matches the confirmed cart.
Describe the receipt as evidence of what was authorised. Do not claim it changes PayPal's liability rules.

[POL-AGENT-1] Agentic purchases are processed through PayPal's existing buyer protection, identity and dispute systems.
Source: https://newsroom.paypal-corp.com/2025-10-28-PayPal-Launches-Agentic-Commerce-Services-to-Power-AI-Driven-Shopping
