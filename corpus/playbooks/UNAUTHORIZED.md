# Playbook: unauthorized transaction

Goal: show the account holder authorised this purchase.
Cite the PayPal buyer approval (the buyer logged in to PayPal and approved the payment), the hosted confirmation
step, the agent identity that requested the quote, and the signed receipt whose seal hash is stored in the PayPal
order's custom_id before payment. If the receipt only shows an agent-relayed confirmation, say that it is weaker.

[POL-UNAUTH-1] Sellers have 10 days to respond to a dispute.
Source: https://www.paypal.com/us/cshelp/article/HELP1083

[POL-UNAUTH-2] PayPal evaluates unauthorized-transaction claims using the payment's authentication and the seller's records of the transaction.
Source: https://www.paypal.com/us/cshelp/article/HELP1083
