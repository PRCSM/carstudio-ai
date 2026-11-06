import { workflow, node, trigger, sticky, newCredential, ifElse, expr } from '@n8n/workflow-sdk';

// CarStudio · Job events → Telegram v2. Handles single-job events (v1 bot / admin approvals)
// and set events (v2 bot). Path differs from v1 so both can exist; N8N_CALLBACK_URL picks one.

const API = 'https://api-production-8ce7.up.railway.app';
const apiCred = { httpHeaderAuth: newCredential('CarStudio API') };
const tgCred = { telegramApi: newCredential('CarStudio Telegram') };

const events = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.2,
  config: {
    name: 'Job events from API',
    parameters: { httpMethod: 'POST', path: 'carstudio-job-events-v2', authentication: 'headerAuth',
                  responseMode: 'onReceived', options: { noResponseBody: true } },
    credentials: apiCred,
  },
});

const isTelegram = ifElse({
  version: 2.2,
  config: {
    name: 'For a Telegram chat?',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [
        { leftValue: expr('{{ $json.body.data.channel }}'), rightValue: 'telegram', operator: { type: 'string', operation: 'equals' } },
        { leftValue: expr('{{ $json.body.data.reply_ref }}'), operator: { type: 'string', operation: 'notEmpty', singleValue: true } },
      ],
      combinator: 'and',
    } },
  },
});

const isSet = ifElse({
  version: 2.2,
  config: {
    name: 'Set event?',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [{ leftValue: expr(`{{ ($json.body.type || '').startsWith('set.') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
      combinator: 'and',
    } },
  },
});

// ---------- set branch ----------
const getSet = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Get set results',
    parameters: {
      method: 'GET',
      url: expr(`${API}/v1/sets/{{ $json.body.data.set_id }}`),
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr('{{ $json.body.data.reply_ref }}') },
        { name: 'X-Channel', value: 'telegram' },
      ] },
      options: { timeout: 30000 },
    },
    credentials: apiCred,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 2000,
  },
});

const anyReady = ifElse({
  version: 2.2,
  config: {
    name: 'Any image ready?',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [{ leftValue: expr(`{{ $('Job events from API').first().json.body.data.completed }}`), rightValue: 0, operator: { type: 'number', operation: 'gt' } }],
      combinator: 'and',
    } },
  },
});

const splitItems = node({
  type: 'n8n-nodes-base.splitOut',
  version: 1,
  config: { name: 'One item per image', parameters: { fieldToSplitOut: 'items', include: 'noOtherFields' } },
});

const hasImage = node({
  type: 'n8n-nodes-base.filter',
  version: 2.2,
  config: {
    name: 'Has an image',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [{ leftValue: expr('{{ $json.url }}'), operator: { type: 'string', operation: 'notEmpty', singleValue: true } }],
      combinator: 'and',
    } },
  },
});

const sendSetPhoto = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send each image',
    parameters: {
      resource: 'message', operation: 'sendPhoto',
      chatId: expr(`{{ $('Job events from API').first().json.body.data.reply_ref }}`),
      binaryData: false,
      file: expr('{{ $json.url }}'),
      additionalFields: {
        caption: expr(`<b>{{ { front: 'Front', front_three_quarter_left: 'Front ¾ · left', front_three_quarter_right: 'Front ¾ · right', left_side: 'Left side', right_side: 'Right side', rear_three_quarter_left: 'Rear ¾ · left', rear_three_quarter_right: 'Rear ¾ · right', rear: 'Rear' }[$json.angle] || ('Photo ' + ($itemIndex + 1)) }}</b>{{ $json.ai_visualization ? '\\n<i>AI visualization: unseen details may differ from the real car.</i>' : '' }}`),
        parse_mode: 'HTML',
        disable_notification: true,
      },
    },
    credentials: tgCred,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 3000,
  },
});

const setSummary = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send set summary',
    parameters: {
      resource: 'message', operation: 'sendMessage',
      chatId: expr(`{{ $('Job events from API').first().json.body.data.reply_ref }}`),
      text: expr(`{{ (() => { const d = $('Job events from API').first().json.body.data; const lines = []; lines.push(d.completed === d.total ? '✨ All ' + d.total + ' ' + (d.mode === 'angles' ? 'angle views' : 'studio photos') + ' are ready!' : (d.completed ? '✨ ' + d.completed + ' of ' + d.total + ' are ready.' : '😕 I couldn\\'t make these images.')); if (d.review_required) lines.push('👀 ' + d.review_required + ' being checked by our team; I\\'ll send them once approved.'); if (d.failed) lines.push('📸 ' + d.failed + ' couldn\\'t be made faithfully. A clearer photo of the whole car usually fixes this.'); if (d.hide_plate) lines.push('🔒 Number plates are blurred.'); lines.push(d.mode === 'angles' ? '<i>Extra angles are AI visualizations; hidden details may differ from the real car.</i>' : '<i>AI-edited: lighting and background are generated; the car is kept as photographed.</i>'); lines.push('Send more photos anytime 📸'); return lines.join('\\n\\n'); })() }}`),
      additionalFields: { appendAttribution: false, parse_mode: 'HTML' },
    },
    credentials: tgCred,
    executeOnce: true,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 3000,
  },
});

const failSummary = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send summary (nothing ready)',
    parameters: {
      resource: 'message', operation: 'sendMessage',
      chatId: expr(`{{ $('Job events from API').first().json.body.data.reply_ref }}`),
      text: expr(`{{ (() => { const d = $('Job events from API').first().json.body.data; const lines = []; lines.push(d.completed === d.total ? '✨ All ' + d.total + ' ' + (d.mode === 'angles' ? 'angle views' : 'studio photos') + ' are ready!' : (d.completed ? '✨ ' + d.completed + ' of ' + d.total + ' are ready.' : '😕 I couldn\\'t make these images.')); if (d.review_required) lines.push('👀 ' + d.review_required + ' being checked by our team; I\\'ll send them once approved.'); if (d.failed) lines.push('📸 ' + d.failed + ' couldn\\'t be made faithfully. A clearer photo of the whole car usually fixes this.'); if (d.hide_plate) lines.push('🔒 Number plates are blurred.'); lines.push(d.mode === 'angles' ? '<i>Extra angles are AI visualizations; hidden details may differ from the real car.</i>' : '<i>AI-edited: lighting and background are generated; the car is kept as photographed.</i>'); lines.push('Send more photos anytime 📸'); return lines.join('\\n\\n'); })() }}`),
      additionalFields: { appendAttribution: false, parse_mode: 'HTML' },
    },
    credentials: tgCred,
    executeOnce: true,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 3000,
  },
});

// ---------- single-job branch (same as v1) ----------
const hasResult = ifElse({
  version: 2.2,
  config: {
    name: 'Image ready?',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [
        { leftValue: expr('{{ $json.body.data.status }}'), rightValue: 'completed', operator: { type: 'string', operation: 'equals' } },
        { leftValue: expr('{{ $json.body.data.has_result }}'), operator: { type: 'boolean', operation: 'true', singleValue: true } },
      ],
      combinator: 'and',
    } },
  },
});

const getResult = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Get result link',
    parameters: {
      method: 'GET',
      url: expr(`${API}/v1/jobs/{{ $json.body.data.job_id }}/results`),
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr('{{ $json.body.data.reply_ref }}') },
        { name: 'X-Channel', value: 'telegram' },
      ] },
      options: { timeout: 30000 },
    },
    credentials: apiCred,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 2000,
  },
});

const sendPhoto = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send studio photo',
    parameters: {
      resource: 'message', operation: 'sendPhoto',
      chatId: expr(`{{ $('Job events from API').item.json.body.data.reply_ref }}`),
      binaryData: false,
      file: expr('{{ $json.images[0].url }}'),
      additionalFields: {
        caption: expr(`✨ Your <b>{{ { white_studio: 'White Studio', dark_luxury: 'Dark Luxury', showroom: 'Showroom' }[$('Job events from API').item.json.body.data.style] || 'studio' }}</b> photo is ready!\n\n<i>AI-edited: lighting and background are generated. The car is kept as photographed.</i>\n\nSend another photo anytime 📸`),
        parse_mode: 'HTML',
      },
    },
    credentials: tgCred,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 3000,
  },
});

const sendUpdate = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send status update',
    parameters: {
      resource: 'message', operation: 'sendMessage',
      chatId: expr('{{ $json.body.data.reply_ref }}'),
      text: expr(`{{ ({ needs_more_images: '📸 ', failed: '😕 ', review_required: '👀 ', cancelled: '🚫 ' }[$json.body.data.status] || '') + ($json.body.data.message || ({ needs_more_images: 'Please send a clearer photo showing the whole car.', failed: 'Sorry, that one did not work. Please try another photo.', review_required: 'Your photo is being checked by our team. I will send it here once approved.', cancelled: 'Your photo was cancelled.' }[$json.body.data.status] || 'Update: ' + $json.body.data.status)) }}`),
      additionalFields: { appendAttribution: false, parse_mode: 'HTML' },
    },
    credentials: tgCred,
    retryOnFail: true, maxTries: 3, waitBetweenTries: 3000,
  },
});

const note = sticky(`## CarStudio AI · Job events → Telegram v2
**set.*** events → fetch the set → send each image (labelled by angle) → one summary.
**job.*** events → same as v1 (single photo or status update).
Active when Railway's N8N_CALLBACK_URL points at /webhook/carstudio-job-events-v2.`, [], { width: 440, height: 200 });

export default workflow('carstudio-job-events-v2', 'CarStudio · Job events → Telegram v2')
  .add(events)
  .to(isTelegram.onTrue(isSet
    .onTrue(getSet.to(anyReady.onTrue(splitItems.to(hasImage.to(sendSetPhoto.to(setSummary)))).onFalse(failSummary)))
    .onFalse(hasResult.onTrue(getResult.to(sendPhoto)).onFalse(sendUpdate))))
  .add(note)
  .group('Deliver a set', [getSet, anyReady, splitItems, hasImage, sendSetPhoto, setSummary, failSummary], { description: 'Sends every finished image of an album or angle set, then one summary message' })
  .group('Deliver a single photo', [hasResult, getResult, sendPhoto, sendUpdate], { description: 'Sends one finished studio photo, or a short update when a retake or review is needed' });
