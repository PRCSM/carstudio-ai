// Live workflow id: x1kqhJH9BI1VADs8
import { workflow, node, trigger, sticky, newCredential, ifElse, switchCase, expr } from '@n8n/workflow-sdk';

// ---------- trigger & routing ----------
const telegramTrigger = trigger({
  type: 'n8n-nodes-base.telegramTrigger',
  version: 1.5,
  config: {
    name: 'Telegram Trigger',
    parameters: {
      updates: ['message', 'callback_query'],
      additionalFields: { download: true, imageSize: 'large' },
    },
    credentials: { telegramApi: newCredential('CarStudio Telegram') },
  },
});

const route = switchCase({
  version: 3.5,
  config: {
    name: 'Route update',
    parameters: {
      mode: 'rules',
      rules: {
        values: [
          {
            renameOutput: true,
            outputKey: 'Style chosen',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ ($json.callback_query?.data || '').startsWith('s|') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Photo',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ !!$json.message?.photo || ($json.message?.document?.mime_type || '').startsWith('image/') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Start / help',
            conditions: {
              options: { caseSensitive: false, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ /^\\/(start|help)\\b/i.test($json.message?.text || '') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Status',
            conditions: {
              options: { caseSensitive: false, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ /^\\/status\\b/i.test($json.message?.text || '') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Delete data',
            conditions: {
              options: { caseSensitive: false, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ /^\\/delete\\b/i.test($json.message?.text || '') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
        ],
      },
      options: { fallbackOutput: 'extra', renameFallbackOutput: 'Anything else' },
    },
  },
});

// ---------- shared bits ----------
const API = 'https://api-production-8ce7.up.railway.app';
const apiCred = { httpHeaderAuth: newCredential('CarStudio API') };
const tgCred = { telegramApi: newCredential('CarStudio Telegram') };
const noAttribution = { appendAttribution: false, parse_mode: 'HTML' };

// ---------- photo → upload → style buttons ----------
const uploadPhoto = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Upload photo to API',
    parameters: {
      method: 'POST',
      url: `${API}/v1/uploads`,
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr('{{ $json.message.chat.id }}') },
        { name: 'X-Channel', value: 'telegram' },
      ] },
      sendBody: true,
      contentType: 'multipart-form-data',
      bodyParameters: { parameters: [{ parameterType: 'formBinaryData', name: 'file', inputDataFieldName: 'data' }] },
      options: { response: { response: { fullResponse: true, neverError: true } }, timeout: 60000 },
    },
    credentials: apiCred,
  },
});

const uploadOk = ifElse({
  version: 2.2,
  config: {
    name: 'Upload accepted?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
        conditions: [{ leftValue: expr('{{ $json.statusCode }}'), rightValue: 201, operator: { type: 'number', operation: 'equals' } }],
        combinator: 'and',
      },
    },
  },
});

const askStyle = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ask for style',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.message.chat.id }}`),
      text: expr(`{{ ($json.body.quality_warnings || []).length ? '⚠️ Heads-up: this photo looks ' + $json.body.quality_warnings.map(w => w.replace('is_', '').replace('too_', 'too ')).join(', ') + '. I\\'ll try, but a sharper, well-lit photo gives a better result.\\n\\n' : '' }}📸 Got your photo! Pick a studio style:`),
      replyMarkup: 'inlineKeyboard',
      inlineKeyboard: { rows: [
        { row: { buttons: [{ text: '⚪ White Studio', additionalFields: { callback_data: expr(`s|white_studio|{{ $json.body.upload_id }}`) } }] } },
        { row: { buttons: [{ text: '⚫ Dark Luxury', additionalFields: { callback_data: expr(`s|dark_luxury|{{ $json.body.upload_id }}`) } }] } },
        { row: { buttons: [{ text: '🏢 Showroom', additionalFields: { callback_data: expr(`s|showroom|{{ $json.body.upload_id }}`) } }] } },
      ] },
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

const uploadRejected = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Explain upload problem',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.message.chat.id }}`),
      text: expr(`😕 {{ $json.body?.error?.message || 'I couldn\\'t read that photo. Please try again with a JPEG or PNG.' }}`),
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

// ---------- style chosen → create job ----------
const ackButton = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Acknowledge button',
    parameters: {
      resource: 'callback',
      operation: 'answerQuery',
      queryId: expr('{{ $json.callback_query.id }}'),
      additionalFields: { text: 'Got it, starting now ✨' },
    },
    credentials: tgCred,
  },
});

const createJob = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Create studio job',
    parameters: {
      method: 'POST',
      url: `${API}/v1/jobs`,
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`) },
        { name: 'X-Channel', value: 'telegram' },
        // same photo + same style = same job, so double-taps don't create duplicates
        { name: 'Idempotency-Key', value: expr(`tg-{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[2] }}-{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] }}`) },
      ] },
      sendBody: true,
      contentType: 'json',
      specifyBody: 'json',
      jsonBody: expr(`{{ JSON.stringify({ source_upload_id: $('Telegram Trigger').item.json.callback_query.data.split('|')[2], style: $('Telegram Trigger').item.json.callback_query.data.split('|')[1], mode: 'same_angle', consent_version: 'tg-v1' }) }}`),
      options: { response: { response: { fullResponse: true, neverError: true } }, timeout: 30000 },
    },
    credentials: apiCred,
  },
});

const jobOk = ifElse({
  version: 2.2,
  config: {
    name: 'Job accepted?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
        conditions: [{ leftValue: expr('{{ $json.statusCode }}'), rightValue: 202, operator: { type: 'number', operation: 'equals' } }],
        combinator: 'and',
      },
    },
  },
});

const working = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Say working on it',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`),
      text: expr(`🎨 Creating your <b>{{ { white_studio: 'White Studio', dark_luxury: 'Dark Luxury', showroom: 'Showroom' }[$('Telegram Trigger').item.json.callback_query.data.split('|')[1]] }}</b> photo… this usually takes 15–60 seconds. I'll send it here when it's ready.`),
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

const jobRejected = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Explain job problem',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`),
      text: expr(`😕 {{ $json.body?.error?.message || 'Something went wrong starting your photo. Please send it again.' }}`),
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

// ---------- commands ----------
const welcome = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send welcome / help',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr('{{ $json.message.chat.id }}'),
      text: `👋 <b>Welcome to CarStudio AI</b>

Send me a photo of your car and pick a style. I'll return a studio-style version of the same shot.

📌 Tips: show the whole car, good light, no heavy blur.

<b>Commands</b>
/status – check your latest photo
/delete – delete all your photos and data
/help – show this message

<i>By sending a photo you agree to it being processed by AI to create a studio image. Lighting and background are AI-generated; the car is kept as photographed. Photos are deleted after 30 days.</i>`,
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

const getStatus = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Get latest job',
    parameters: {
      method: 'GET',
      url: `${API}/v1/me/jobs/latest`,
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr('{{ $json.message.chat.id }}') },
        { name: 'X-Channel', value: 'telegram' },
      ] },
      options: { response: { response: { fullResponse: true, neverError: true } } },
    },
    credentials: apiCred,
  },
});

const sendStatus = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Send status',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.message.chat.id }}`),
      text: expr(`{{ $json.statusCode === 200 ? ({ queued: '⏳ Waiting in line', validating: '🔍 Checking your photo', analyzing: '🔍 Checking your photo', preparing: '🎨 Preparing', generating: '🎨 Creating your studio photo', verifying: '✅ Quality-checking', completed: '✅ Done, it was sent above', failed: '❌ That one failed', cancelled: '🚫 Cancelled', review_required: '👀 Being checked by our team', needs_more_images: '📸 We need a clearer photo' }[$json.body.status] || $json.body.status) + ($json.body.message ? '\\n' + $json.body.message : '') : 'You haven\\'t sent any photos yet. Send one to get started 📸' }}`),
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

const deleteData = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Delete my data',
    parameters: {
      method: 'DELETE',
      url: `${API}/v1/me/data`,
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr('{{ $json.message.chat.id }}') },
        { name: 'X-Channel', value: 'telegram' },
      ] },
      options: { response: { response: { fullResponse: true, neverError: true } } },
    },
    credentials: apiCred,
  },
});

const confirmDelete = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Confirm deletion',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.message.chat.id }}`),
      text: expr(`{{ $json.statusCode === 204 ? '🗑️ Done. All your photos, results and data have been deleted.' : '😕 I couldn\\'t delete your data right now. Please try /delete again in a minute.' }}`),
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

const fallback = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ask for a photo',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr('{{ $json.message?.chat?.id || $json.callback_query?.message?.chat?.id }}'),
      text: 'Send me a photo of your car 📸 and I\'ll turn it into a studio shot. Type /help for more.',
      additionalFields: noAttribution,
    },
    credentials: tgCred,
  },
});

const note = sticky(`## CarStudio AI · Telegram bot
1. Customer sends a car photo → uploaded to the API → bot offers 3 style buttons.
2. Tapping a style creates a job (same photo + style = same job, so double taps are safe).
3. The finished image is sent by the **Job events → Telegram** workflow.

Credentials: **CarStudio Telegram** (bot token) and **CarStudio API** (Header Auth: Authorization = Bearer <N8N_INTERNAL_API_TOKEN>).`, [], { width: 420, height: 260 });

export default workflow('carstudio-telegram-bot', 'CarStudio · Telegram bot')
  .add(telegramTrigger)
  .to(route
    .onCase(0, ackButton.to(createJob.to(jobOk.onTrue(working).onFalse(jobRejected))))
    .onCase(1, uploadPhoto.to(uploadOk.onTrue(askStyle).onFalse(uploadRejected)))
    .onCase(2, welcome)
    .onCase(3, getStatus.to(sendStatus))
    .onCase(4, deleteData.to(confirmDelete))
    .onCase(5, fallback))
  .add(note)
  .group('Photo → style buttons', [uploadPhoto, uploadOk, askStyle, uploadRejected], { description: 'Uploads the photo to the CarStudio API and offers White Studio / Dark Luxury / Showroom buttons' })
  .group('Style chosen → start job', [ackButton, createJob, jobOk, working, jobRejected], { description: 'Creates the studio job for the chosen style and tells the customer it is in progress' })
  .group('Status', [getStatus, sendStatus], { description: 'Replies to /status with the latest job state' })
  .group('Delete data', [deleteData, confirmDelete], { description: 'Replies to /delete by erasing all of the customer\'s photos and data' });
