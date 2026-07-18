# Growthable — Email Brand Guide for AI

You are generating marketing emails for **Growthable** (growthable.io), sent via **Resend**. Follow this guide exactly. Output raw HTML emails (table-based, inline styles only) using the skeleton and components below. Do not invent new colors, fonts, or layouts.

## 1. Who we are

Growthable gives GoHighLevel agencies an AI (Co-Pilot) that runs client onboarding and support calls live, plus 24/7 whitelabel support, done-for-you builds, compliance filing (A2P/KYC), a grey-label GHL course library, and software that extends HighLevel (ghladmin). One partner for the whole back office. Audience: agency owners running GoHighLevel — busy, technical enough, allergic to fluff.

Key proof points to reuse (never invent new ones):
- 24/7/365 live client support, under the agency's brand
- 853+ free GHL tutorials
- Live in 1–2 business days
- Unlimited sub-accounts, flat rate, from $549/month, no contracts
- Compliance filed for US (A2P 10DLC), AU (regulatory bundles), UK (KYC)
- Co-Pilot: AI that guides clients live on screen-share, trained on the agency's videos, docs & SOPs

## 2. Brand tokens

Colors (use these hex values verbatim):
- `#F03E6A` — Growthable Pink. CTAs, links, kickers, accents. Never body text.
- `#34475B` — Growthable Navy. Headings, dark bands, footers' bold text.
- `#52606D` — Body text on white.
- `#8894A0` — Muted text: footers, fine print, captions.
- `#C2CAD4` — Body text on navy backgrounds.
- `#F58BA6` — Kicker/accent text on navy backgrounds (pink is too dark on navy).
- `#FDE8EE` — Pink tint. Pill backgrounds, step-number circles.
- `#F5F6F8` — Email canvas background.
- `#FFFFFF` — Card background.
- `#E4E8EC` — Hairline dividers.

Typography — email-safe stack, always inline:
`font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;`
- H1: 28–32px / line-height +6px / weight 800 / navy
- H2 (card/story titles): 17–20px / weight 700 / navy
- Body: 15–16px / line-height 24–26px / #52606D
- Kicker: 12px / weight 700 / letter-spacing 1.5px / uppercase / pink
- Fine print & footer: 13px / #8894A0
- Never use webfonts, Google Fonts, or font sizes below 13px.

Logos (host these; see §7):
- `https://growthableemail.onrender.com/assets/growthable-logo.png` — full wordmark, for white/light backgrounds. Render at width 140–160px.
- `https://growthableemail.onrender.com/assets/growthable-logo-white.png` — white wordmark, ONLY on navy (#34475B) backgrounds.
- `https://growthableemail.onrender.com/assets/growthable-icon.png` — "g" arrow icon, square. Avatars/favicons only, never as the header logo.
- Always `alt="Growthable"`, `style="display:block; border:0; height:auto;"`, explicit `width` attribute. Never stretch, recolor, rotate, or put the color logo on navy.

## 3. Voice & copy

Tone: confident, direct, a bit cheeky. We sell time back, not software features.
- Short sentences. Second person. Contractions. Active voice.
- Lead with the outcome ("Your week stops disappearing into how-do-I questions"), then the mechanism.
- Cheeky is one wink per email, not a stand-up set. Example register: "You go grow." / "Agencies don't rave about support. They do about ours."
- Numbers beat adjectives: "Resolved in 4 minutes — while you slept" > "lightning-fast support".
- Emoji: at most one, only in casual lifecycle emails (👋 😬 👍 range). Never in subject lines for promos, never more than one.
- Never: "revolutionize", "unlock", "supercharge", "game-changer", exclamation marks in headlines, ALL CAPS urgency.
- Every email has exactly ONE primary CTA. Secondary action is a text link, not a second button.
- CTA copy is a verb + object, 2–4 words: "Try Co-Pilot free →", "Browse the library", "Claim 50% off →". Arrows (→) are on-brand.

## 4. Subject lines & preheaders

Subjects: ≤50 chars, sentence case, no clickbait, no ALL CAPS, ≤1 emoji ever (prefer none).
Formulas that fit the brand:
- Outcome, blunt: "Stop answering the same questions"
- Number + noun: "853 tutorials. Zero fluff."
- New thing, plainly: "Co-Pilot is here"
- Deadline, honest: "50% off ends Friday"
- Lifecycle, personal: "Your first 48 hours with Growthable"

Preheader: 40–90 chars, extends (never repeats) the subject. Always include the hidden preheader div (see skeleton) — end it with the `&nbsp;&zwnj;` padding run so inbox previews don't leak body text.

## 5. Layout system

Every email: 600px table on #F5F6F8 canvas → white card(s) with border-radius 12px and 40px padding → gray footer text below the card.
- Header: full-color logo, width 150, top-left, 24px below it the card starts. Exception: promo emails put the WHITE logo inside a navy hero instead.
- Navy (#34475B) bands: max one per email — either a promo hero or a bottom CTA band. Never two.
- Buttons: pink background, border-radius 8px, white bold 15–16px text, padding 13–14px vertical / 28–36px horizontal. Built as a nested table (see component).
- Feature lists: pink "→" or "✓" glyph column (28px wide) + body text. 10–12px between rows.
- Dividers between repeated items: 1px solid #E4E8EC, 24px padding above and below.
- Images: full column width (520px inside a 40px-padded card), border-radius 8px, meaningful alt text, explicit width attr. Reference hosted screenshots; never data-URIs.
- Keep total email height modest: 3 stories max in a newsletter, 3–4 bullets max in a list.

## 6. Templates

Four production templates. Copy the closest one and edit content only — structure, spacing and colors stay.

### Template: Newsletter / tutorial roundup ("The Growth Brief")

File: `email-assets/templates/newsletter.html`. Weekly/monthly digest. Also the pattern for tutorial & educational roundups: swap the three story blocks.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>The Growth Brief</title>
</head>
<body style="margin:0; padding:0; background-color:#F5F6F8; -webkit-text-size-adjust:100%;">
<!-- Preheader: shows in inbox preview, hidden in email body -->
<div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">Three things making agencies faster this week — plus new tutorials from the library.&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F5F6F8;">
<tr><td align="center" style="padding:32px 16px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px; max-width:100%;">

    <!-- Header -->
    <tr><td style="padding:0 8px 24px 8px;">
      <a href="https://growthable.io" style="text-decoration:none;">
        <img src="https://growthableemail.onrender.com/assets/growthable-logo.png" width="150" alt="Growthable" style="display:block; border:0; width:150px; height:auto;">
      </a>
    </td></tr>

    <!-- Card -->
    <tr><td style="background-color:#FFFFFF; border-radius:12px; padding:40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">

        <!-- Kicker + title -->
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:12px; font-weight:700; letter-spacing:1.5px; text-transform:uppercase; color:#F03E6A; padding-bottom:12px;">The Growth Brief · July 2026</td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:28px; line-height:34px; font-weight:800; color:#34475B; padding-bottom:16px;">Three things making agencies faster this week</td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:28px;">Your week shouldn't disappear into how-do-I questions. Here's what's new, what's working, and what to steal.</td></tr>

        <!-- Story 1 -->
        <tr><td style="border-top:1px solid #E4E8EC; padding:24px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:18px; line-height:24px; font-weight:700; color:#34475B; padding-bottom:8px;">Client onboarding checklists that prevent confusion</td></tr>
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D; padding-bottom:10px;">Stop confusion before it starts. A structured checklist means fewer 2am "how do I connect my domain?" messages.</td></tr>
            <tr><td><a href="https://growthable.io/gohighlevel-tutorials/" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:700; color:#F03E6A; text-decoration:none;">Read the guide →</a></td></tr>
          </table>
        </td></tr>

        <!-- Story 2 -->
        <tr><td style="border-top:1px solid #E4E8EC; padding:24px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:18px; line-height:24px; font-weight:700; color:#34475B; padding-bottom:8px;">SMS compliance: sender ID &amp; opt-out setup</td></tr>
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D; padding-bottom:10px;">Carrier red tape stalls agencies for weeks. Get sender IDs and opt-out language right the first time.</td></tr>
            <tr><td><a href="https://growthable.io/gohighlevel-tutorials/" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:700; color:#F03E6A; text-decoration:none;">Read the guide →</a></td></tr>
          </table>
        </td></tr>

        <!-- Story 3 -->
        <tr><td style="border-top:1px solid #E4E8EC; padding:24px 0 8px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:18px; line-height:24px; font-weight:700; color:#34475B; padding-bottom:8px;">Fix bad call quality in GoHighLevel</td></tr>
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D; padding-bottom:10px;">The full troubleshooting guide — from codec settings to carrier routes.</td></tr>
            <tr><td><a href="https://growthable.io/gohighlevel-tutorials/" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:700; color:#F03E6A; text-decoration:none;">Read the guide →</a></td></tr>
          </table>
        </td></tr>

      </table>
    </td></tr>

    <!-- CTA band -->
    <tr><td style="padding-top:16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#34475B; border-radius:12px;">
        <tr><td style="padding:32px 40px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:20px; line-height:26px; font-weight:800; color:#FFFFFF; padding-bottom:8px;">853+ free GHL tutorials, and counting</td></tr>
            <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#C2CAD4; padding-bottom:20px;">New every week. Browse the full library whenever you're stuck.</td></tr>
            <tr><td>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr><td style="background-color:#F03E6A; border-radius:8px;">
                  <a href="https://growthable.io/gohighlevel-tutorials/" style="display:inline-block; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:700; color:#FFFFFF; text-decoration:none; padding:13px 28px;">Browse the library</a>
                </td></tr>
              </table>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </td></tr>

    <!-- Footer -->
    <tr><td style="padding:32px 8px 0 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0; padding-bottom:8px;">Growthable LLC · 1942 Broadway St STE 314C, Boulder CO 80302, US · +1 910-839-7618 · Whitelabel support, onboarding &amp; training for GoHighLevel agencies</td></tr>
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0;">
          <a href="https://growthable.io" style="color:#8894A0; text-decoration:underline;">growthable.io</a> &nbsp;·&nbsp; <a href="{{unsubscribe_url}}" style="color:#8894A0; text-decoration:underline;">Unsubscribe</a> &nbsp;·&nbsp; <a href="{{preferences_url}}" style="color:#8894A0; text-decoration:underline;">Email preferences</a>
        </td></tr>
      </table>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>
```

### Template: Product announcement

File: `email-assets/templates/product-announcement.html`. New feature/product launches. Also the pattern for event & webinar invites: swap the screenshot for a date/time block and the CTA to "Save my seat".

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>Meet Co-Pilot</title>
</head>
<body style="margin:0; padding:0; background-color:#F5F6F8; -webkit-text-size-adjust:100%;">
<div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">An AI that runs onboarding and support calls live — trained on your product.&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F5F6F8;">
<tr><td align="center" style="padding:32px 16px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px; max-width:100%;">

    <!-- Header -->
    <tr><td style="padding:0 8px 24px 8px;">
      <a href="https://growthable.io" style="text-decoration:none;">
        <img src="https://growthableemail.onrender.com/assets/growthable-logo.png" width="150" alt="Growthable" style="display:block; border:0; width:150px; height:auto;">
      </a>
    </td></tr>

    <!-- Card -->
    <tr><td style="background-color:#FFFFFF; border-radius:12px; padding:48px 40px 40px 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">

        <!-- NEW pill -->
        <tr><td style="padding-bottom:16px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="background-color:#FDE8EE; border-radius:999px; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:12px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:#F03E6A; padding:6px 14px;">New · Co-Pilot</td></tr>
          </table>
        </td></tr>

        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:32px; line-height:38px; font-weight:800; color:#34475B; padding-bottom:16px;">Your agency's AI teammate is here</td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:24px;">Co-Pilot watches a client's screen, points to the exact next step, and tells them what to do — out loud, live. Trained on your product, so your team stops answering the same questions.</td></tr>

        <!-- Product image -->
        <tr><td style="padding-bottom:28px;">
          <img src="https://growthable.io/brand/email/copilot-screenshot.png" width="520" alt="Co-Pilot guiding a client through GoHighLevel on a live screen-share" style="display:block; border:0; width:100%; max-width:520px; height:auto; border-radius:8px;">
        </td></tr>

        <!-- Feature list -->
        <tr><td style="padding-bottom:8px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">→</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:12px;">Runs guided onboarding and support calls, live on screen-share</td>
            </tr>
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">→</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:12px;">Trained on your videos, docs &amp; SOPs — answers like your team</td>
            </tr>
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">→</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:24px;">Free for every agency to try</td>
            </tr>
          </table>
        </td></tr>

        <!-- CTA -->
        <tr><td>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="background-color:#F03E6A; border-radius:8px;">
              <a href="https://growthable.io/co-pilot/" style="display:inline-block; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; font-weight:700; color:#FFFFFF; text-decoration:none; padding:14px 32px;">Try Co-Pilot free →</a>
            </td></tr>
          </table>
        </td></tr>
        <tr><td style="padding-top:16px;">
          <a href="https://growthable.io/co-pilot/" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:600; color:#34475B; text-decoration:underline;">Or watch the 2-minute demo</a>
        </td></tr>

      </table>
    </td></tr>

    <!-- Footer -->
    <tr><td style="padding:32px 8px 0 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0; padding-bottom:8px;">Growthable LLC · 1942 Broadway St STE 314C, Boulder CO 80302, US · +1 910-839-7618 · Whitelabel support, onboarding &amp; training for GoHighLevel agencies</td></tr>
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0;">
          <a href="https://growthable.io" style="color:#8894A0; text-decoration:underline;">growthable.io</a> &nbsp;·&nbsp; <a href="{{unsubscribe_url}}" style="color:#8894A0; text-decoration:underline;">Unsubscribe</a> &nbsp;·&nbsp; <a href="{{preferences_url}}" style="color:#8894A0; text-decoration:underline;">Email preferences</a>
        </td></tr>
      </table>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>
```

### Template: Promotional / offer

File: `email-assets/templates/promo-offer.html`. The only template with a navy hero. Use sparingly — offers, deadlines, big moments.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>Flat pricing. Unlimited sub-accounts.</title>
</head>
<body style="margin:0; padding:0; background-color:#F5F6F8; -webkit-text-size-adjust:100%;">
<div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">No pay-per-session fees. No tiers that punish growth. Offer ends Friday.&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F5F6F8;">
<tr><td align="center" style="padding:32px 16px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px; max-width:100%;">

    <!-- Navy hero with white logo -->
    <tr><td style="background-color:#34475B; border-radius:12px 12px 0 0; padding:40px 40px 36px 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td style="padding-bottom:28px;">
          <a href="https://growthable.io" style="text-decoration:none;">
            <img src="https://growthableemail.onrender.com/assets/growthable-logo-white.png" width="150" alt="Growthable" style="display:block; border:0; width:150px; height:auto;">
          </a>
        </td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:12px; font-weight:700; letter-spacing:1.5px; text-transform:uppercase; color:#F58BA6; padding-bottom:12px;">Limited time · Ends Friday</td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:32px; line-height:38px; font-weight:800; color:#FFFFFF; padding-bottom:14px;">Hand off support this week.<br>First month 50% off.</td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#C2CAD4;">One flat rate. Unlimited sub-accounts. No contracts — love us or leave anytime.</td></tr>
      </table>
    </td></tr>

    <!-- White body -->
    <tr><td style="background-color:#FFFFFF; border-radius:0 0 12px 12px; padding:36px 40px 40px 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">

        <!-- What's included -->
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:#8894A0; padding-bottom:16px;">Every plan includes</td></tr>
        <tr><td style="padding-bottom:24px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">✓</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:10px;">24/7/365 live chat, tickets &amp; Zoom — under your brand</td>
            </tr>
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">✓</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:10px;">Done-for-you client onboarding sessions</td>
            </tr>
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">✓</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:10px;">A2P, KYC &amp; carrier compliance, filed for you</td>
            </tr>
            <tr>
              <td width="28" valign="top" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#F03E6A; font-weight:700;">✓</td>
              <td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D;">$2,000 grey-label GHL course library</td>
            </tr>
          </table>
        </td></tr>

        <!-- CTA -->
        <tr><td align="center" style="padding-bottom:14px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="background-color:#F03E6A; border-radius:8px;">
              <a href="https://growthable.io/pricing/" style="display:inline-block; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; font-weight:700; color:#FFFFFF; text-decoration:none; padding:14px 36px;">Claim 50% off →</a>
            </td></tr>
          </table>
        </td></tr>
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0;">Applies to your first month on any plan. New customers only.</td></tr>

      </table>
    </td></tr>

    <!-- Footer -->
    <tr><td style="padding:32px 8px 0 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0; padding-bottom:8px;">Growthable LLC · 1942 Broadway St STE 314C, Boulder CO 80302, US · +1 910-839-7618 · Whitelabel support, onboarding &amp; training for GoHighLevel agencies</td></tr>
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0;">
          <a href="https://growthable.io" style="color:#8894A0; text-decoration:underline;">growthable.io</a> &nbsp;·&nbsp; <a href="{{unsubscribe_url}}" style="color:#8894A0; text-decoration:underline;">Unsubscribe</a> &nbsp;·&nbsp; <a href="{{preferences_url}}" style="color:#8894A0; text-decoration:underline;">Email preferences</a>
        </td></tr>
      </table>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>
```

### Template: Onboarding / lifecycle

File: `email-assets/templates/onboarding-welcome.html`. Welcome + step sequences. Personal, plain, reply-friendly. Pattern for the whole lifecycle series.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>Welcome to Growthable</title>
</head>
<body style="margin:0; padding:0; background-color:#F5F6F8; -webkit-text-size-adjust:100%;">
<div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">Three steps and your clients have 24/7 support — most agencies are live in 1–2 days.&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F5F6F8;">
<tr><td align="center" style="padding:32px 16px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px; max-width:100%;">

    <!-- Header -->
    <tr><td style="padding:0 8px 24px 8px;">
      <a href="https://growthable.io" style="text-decoration:none;">
        <img src="https://growthableemail.onrender.com/assets/growthable-logo.png" width="150" alt="Growthable" style="display:block; border:0; width:150px; height:auto;">
      </a>
    </td></tr>

    <!-- Card -->
    <tr><td style="background-color:#FFFFFF; border-radius:12px; padding:48px 40px 40px 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">

        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:28px; line-height:34px; font-weight:800; color:#34475B; padding-bottom:16px;">Welcome aboard, {{first_name}} 👋</td></tr>
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; line-height:26px; color:#52606D; padding-bottom:28px;">You just handed off the part of your week that grows nothing. Most agencies are fully live in 1–2 business days — here's how to get there.</td></tr>

        <!-- Step 1 -->
        <tr><td style="padding-bottom:20px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="44" valign="top">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                  <tr><td align="center" style="width:32px; height:32px; background-color:#FDE8EE; border-radius:999px; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:800; color:#F03E6A;">1</td></tr>
                </table>
              </td>
              <td>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:17px; line-height:24px; font-weight:700; color:#34475B; padding-bottom:4px;">Connect your agency account</td></tr>
                  <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D;">Takes minutes. No migration, no rebuild.</td></tr>
                </table>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- Step 2 -->
        <tr><td style="padding-bottom:20px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="44" valign="top">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                  <tr><td align="center" style="width:32px; height:32px; background-color:#FDE8EE; border-radius:999px; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:800; color:#F03E6A;">2</td></tr>
                </table>
              </td>
              <td>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:17px; line-height:24px; font-weight:700; color:#34475B; padding-bottom:4px;">Drop the widget into your sub-accounts</td></tr>
                  <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D;">Your clients get live chat, tickets and onboarding — under your brand.</td></tr>
                </table>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- Step 3 -->
        <tr><td style="padding-bottom:28px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="44" valign="top">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                  <tr><td align="center" style="width:32px; height:32px; background-color:#FDE8EE; border-radius:999px; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; font-weight:800; color:#F03E6A;">3</td></tr>
                </table>
              </td>
              <td>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:17px; line-height:24px; font-weight:700; color:#34475B; padding-bottom:4px;">You go grow</td></tr>
                  <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D;">Spend the week selling and building — we've got the how-do-I questions.</td></tr>
                </table>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- CTA -->
        <tr><td>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="background-color:#F03E6A; border-radius:8px;">
              <a href="https://growthable.io" style="display:inline-block; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:16px; font-weight:700; color:#FFFFFF; text-decoration:none; padding:14px 32px;">Start step 1 →</a>
            </td></tr>
          </table>
        </td></tr>

        <!-- Sign-off -->
        <tr><td style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; color:#52606D; padding-top:28px;">Questions? Just reply — a real person reads every email.<br><span style="color:#34475B; font-weight:700;">— The Growthable team</span></td></tr>

      </table>
    </td></tr>

    <!-- Footer -->
    <tr><td style="padding:32px 8px 0 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0; padding-bottom:8px;">Growthable LLC · 1942 Broadway St STE 314C, Boulder CO 80302, US · +1 910-839-7618 · Whitelabel support, onboarding &amp; training for GoHighLevel agencies</td></tr>
        <tr><td align="center" style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif; font-size:13px; line-height:20px; color:#8894A0;">
          <a href="https://growthable.io" style="color:#8894A0; text-decoration:underline;">growthable.io</a> &nbsp;·&nbsp; <a href="{{unsubscribe_url}}" style="color:#8894A0; text-decoration:underline;">Unsubscribe</a> &nbsp;·&nbsp; <a href="{{preferences_url}}" style="color:#8894A0; text-decoration:underline;">Email preferences</a>
        </td></tr>
      </table>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>
```

## 7. Sending via Resend

- Send with the `html` field (these are complete documents). From: `Growthable <hello@growthable.io>` or similar verified domain sender. Friendly from-name is "Growthable" — not "Growthable Team", not a person's name unless it's a genuine founder note.
- Replace `{{first_name}}`, `{{unsubscribe_url}}`, `{{preferences_url}}` with real merge values before sending. If first name is unknown, drop the personalization gracefully ("Welcome aboard 👋").
- Images must be hosted at public HTTPS URLs. Upload `email-assets/logo-full.png`, `email-assets/logo-white.png`, `email-assets/icon.jpg` to `https://growthable.io/brand/email/` (or any CDN) and keep the URLs in this guide's format. PNG with transparency for logos; JPG/PNG under 200KB for photos.
- Always set both `html` and a plain-text `text` fallback (short summary + primary link).
- Footer is legally required: company name (Growthable LLC), physical mailing address (add yours where the descriptor line is), and a working unsubscribe link. Resend's `{{{RESEND_UNSUBSCRIBE_URL}}}` can replace `{{unsubscribe_url}}` when using Broadcasts.

## 8. Technical & deliverability rules

- Tables only (`role="presentation"`), inline styles only. No `<style>` blocks except nothing, no classes, no flexbox/grid, no JS, no forms, no video.
- Width 600px, `max-width:100%` for mobile. Single column always.
- Dark mode: don't fight it. Solid hex backgrounds on every band (never rely on body bg), the white-logo-on-navy rule keeps the hero legible, and pink #F03E6A holds up in both modes. Never use pure black text.
- Every `<img>`: alt text, `display:block`, explicit width, hosted HTTPS src.
- Links: full https URLs to growthable.io pages. Pink #F03E6A for inline text links on white; underlined gray in footers.
- Text-to-image ratio: emails must read fine with images off. Never image-only emails.
- Outlook: the nested-table button pattern above is the supported one; radius may flatten in old Outlook — acceptable.

## 9. Do / Don't

DO: one CTA per email · pink for action only · navy for weight · white space over boxes · specific numbers · short subjects · one column.
DON'T: gradients · more than one navy band · centered long paragraphs · pink body text · color logo on dark backgrounds · webfonts · multiple buttons · emoji pile-ups · fake urgency ("LAST CHANCE!!!") · inventing stats or testimonials.
