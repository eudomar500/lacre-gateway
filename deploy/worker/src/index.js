// Email Worker for Lacre mailboxes.
//
// Email Routing hands every message sent to the zone to this Worker (the
// catch-all rule). A message for lacre-<id>@<LACRE_MAIL_DOMAIN> is passed to
// the gateway byte for byte, signed with LACRE_INBOUND_SECRET; anything else
// is rejected here and never leaves Cloudflare.
//
// Nothing of a message is logged: not its headers, not its body, not the
// sender and not the full recipient. One line per message says which
// mailbox id it was for, its size and what the gateway answered.

const encoder = new TextEncoder();

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function recipientPattern(domain) {
  return new RegExp("^lacre-([a-z2-7]{12})@" + escapeRegExp(domain) + "$");
}

function hex(buffer) {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

// The gateway checks the same MAC (lacre_gateway/app.py, inbound_mac). The
// timestamp and recipient are signed with the message so a captured request
// cannot be sent again later, or to another mailbox, under a new timestamp.
async function sign(secret, timestamp, recipient, raw) {
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const prefix = encoder.encode(timestamp + "\n" + recipient + "\n");
  const data = new Uint8Array(prefix.length + raw.length);
  data.set(prefix, 0);
  data.set(raw, prefix.length);
  return hex(await crypto.subtle.sign("HMAC", key, data));
}

function note(mailbox, size, status) {
  console.log(JSON.stringify({ mailbox, size, status }));
}

export default {
  async email(message, env, ctx) {
    const domain = String(env.LACRE_MAIL_DOMAIN || "").toLowerCase();
    // Local parts are case-insensitive in practice; ids are issued in lower
    // case, so the address is compared in lower case too.
    const recipient = String(message.to || "").toLowerCase();
    const size = message.rawSize;
    const match = domain ? recipientPattern(domain).exec(recipient) : null;
    if (!match) {
      // Checked here so mail for any other address on the zone is never
      // read, signed or sent to the gateway.
      note(null, size, "not a mailbox address");
      message.setReject("No such mailbox");
      return;
    }
    const mailbox = match[1];
    const limit = Number(env.LACRE_MAX_EML_BYTES || 10485760);
    if (size > limit) {
      note(mailbox, size, "too large");
      message.setReject("Message too large");
      return;
    }
    if (!env.LACRE_INBOUND_SECRET || !env.LACRE_INBOUND_URL) {
      note(mailbox, size, "not configured");
      message.setReject("Mailbox unavailable");
      return;
    }

    let status;
    try {
      const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
      const timestamp = String(Math.floor(Date.now() / 1000));
      const signature = await sign(env.LACRE_INBOUND_SECRET, timestamp, recipient, raw);
      const response = await fetch(env.LACRE_INBOUND_URL, {
        method: "POST",
        headers: {
          "Content-Type": "message/rfc822",
          "X-Lacre-Signature": signature,
          "X-Lacre-Recipient": recipient,
          "X-Lacre-Timestamp": timestamp,
        },
        body: raw,
        signal: AbortSignal.timeout(30000),
      });
      status = response.status;
    } catch (error) {
      // A gateway that cannot be reached is treated like one that said no:
      // the sender gets a bounce instead of mail that silently vanished.
      status = "unreachable";
    }
    note(mailbox, size, status);
    if (typeof status !== "number" || status < 200 || status > 299) {
      message.setReject("Mailbox could not accept the message");
    }
  },
};
