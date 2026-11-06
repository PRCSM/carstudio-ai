import { workflow, node, trigger, sticky, newCredential, ifElse, switchCase, expr } from '@n8n/workflow-sdk';

// CarStudio · Telegram bot v2 — photo or album → mode → style → plate → set (POST /v1/sets).
// Callback data (<=64 bytes): m|<e|a>|<group>   st|<e|a>|<ws|dl|sr>|<group>   pl|<e|a>|<style>|<0|1>|<group>

const API = 'https://api-production-8ce7.up.railway.app';
const apiCred = { httpHeaderAuth: newCredential('CarStudio API') };
const tgCred = { telegramApi: newCredential('CarStudio Telegram') };
const html = { appendAttribution: false, parse_mode: 'HTML' };

const tgTrigger = trigger({
  type: 'n8n-nodes-base.telegramTrigger',
  version: 1.5,
  config: {
    name: 'Telegram Trigger',
    parameters: { updates: ['message', 'callback_query'], additionalFields: { download: true, imageSize: 'extraLarge' } },
    credentials: tgCred,
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
            outputKey: 'Mode chosen',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ ($json.callback_query?.data || '').startsWith('m|') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Style chosen',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ ($json.callback_query?.data || '').startsWith('st|') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Plate chosen',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ ($json.callback_query?.data || '').startsWith('pl|') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
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
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ /^\\/(start|help)\\b/i.test($json.message?.text || '') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Status',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
              conditions: [{ leftValue: expr(`{{ /^\\/status\\b/i.test($json.message?.text || '') }}`), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
              combinator: 'and',
            },
          },
          {
            renameOutput: true,
            outputKey: 'Delete data',
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
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

// ---------- 1. photo or album → upload → (first photo only) ask what to do ----------
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
        { name: 'X-Media-Group', value: expr(`{{ $json.message.media_group_id || '' }}`) },
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
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [{ leftValue: expr('{{ $json.statusCode }}'), rightValue: 201, operator: { type: 'number', operation: 'equals' } }],
      combinator: 'and',
    } },
  },
});

const firstOfAlbum = ifElse({
  version: 2.2,
  config: {
    name: 'First photo of the album?',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [{ leftValue: expr('{{ $json.body.is_first_in_group }}'), operator: { type: 'boolean', operation: 'true', singleValue: true } }],
      combinator: 'and',
    } },
  },
});

const askMode = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ask what to make',
    parameters: {
      resource: 'message',
      operation: 'sendMessage',
      chatId: expr(`{{ $('Telegram Trigger').item.json.message.chat.id }}`),
      text: expr(`{{ $('Telegram Trigger').item.json.message.media_group_id ? '📸 Got your photos!' : '📸 Got your photo!' }}{{ ($json.body.quality_warnings || []).length ? ' ⚠️ It looks a bit ' + $json.body.quality_warnings.map(w => w.replace('is_', '').replace('too_', 'too ')).join(', ') + ', so results may be weaker.' : '' }}

What would you like?

✨ <b>Studio-edit</b>: your real photo(s) restyled in a studio. Every detail stays real.
🔄 <b>Extra angles</b>: AI creates front, back, side and angled views from your photo(s). <i>These are visualizations; hidden details may differ.</i>`),
      replyMarkup: 'inlineKeyboard',
      inlineKeyboard: { rows: [
        { row: { buttons: [{ text: '✨ Studio-edit my photos', additionalFields: { callback_data: expr('m|e|{{ $json.body.group_id }}') } }] } },
        { row: { buttons: [{ text: '🔄 Create extra angles (AI)', additionalFields: { callback_data: expr('m|a|{{ $json.body.group_id }}') } }] } },
      ] },
      additionalFields: html,
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
      additionalFields: html,
    },
    credentials: tgCred,
  },
});

// ---------- 2. mode chosen → ask style ----------
const ackMode = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ack mode',
    parameters: { resource: 'callback', operation: 'answerQuery', queryId: expr('{{ $json.callback_query.id }}') },
    credentials: tgCred,
  },
});

const askStyle = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ask for style',
    parameters: {
      resource: 'message',
      operation: 'editMessageText',
      messageType: 'message',
      chatId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`),
      messageId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.message_id }}`),
      text: expr(`{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] === 'a' ? '🔄 Extra angles' : '✨ Studio-edit' }}: pick a studio style`),
      replyMarkup: 'inlineKeyboard',
      inlineKeyboard: { rows: [
        { row: { buttons: [{ text: '⚪ White Studio', additionalFields: { callback_data: expr(`st|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] }}|ws|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[2] }}`) } }] } },
        { row: { buttons: [{ text: '⚫ Dark Luxury', additionalFields: { callback_data: expr(`st|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] }}|dl|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[2] }}`) } }] } },
        { row: { buttons: [{ text: '🏢 Showroom', additionalFields: { callback_data: expr(`st|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] }}|sr|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[2] }}`) } }] } },
      ] },
      additionalFields: { parse_mode: 'HTML' },
    },
    credentials: tgCred,
  },
});

// ---------- 3. style chosen → ask about plate ----------
const ackStyle = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ack style',
    parameters: { resource: 'callback', operation: 'answerQuery', queryId: expr('{{ $json.callback_query.id }}') },
    credentials: tgCred,
  },
});

const askPlate = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ask about number plate',
    parameters: {
      resource: 'message',
      operation: 'editMessageText',
      messageType: 'message',
      chatId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`),
      messageId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.message_id }}`),
      text: '🔒 Hide the number plate in the results?',
      replyMarkup: 'inlineKeyboard',
      inlineKeyboard: { rows: [
        { row: { buttons: [{ text: '🔒 Yes, blur it', additionalFields: { callback_data: expr(`pl|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] }}|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[2] }}|1|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[3] }}`) } }] } },
        { row: { buttons: [{ text: '👁️ No, keep it visible', additionalFields: { callback_data: expr(`pl|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] }}|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[2] }}|0|{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[3] }}`) } }] } },
      ] },
      additionalFields: { parse_mode: 'HTML' },
    },
    credentials: tgCred,
  },
});

// ---------- 4. plate chosen → create the set ----------
const ackPlate = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Ack plate',
    parameters: { resource: 'callback', operation: 'answerQuery', queryId: expr('{{ $json.callback_query.id }}'),
                  additionalFields: { text: 'Starting now ✨' } },
    credentials: tgCred,
  },
});

const createSet = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.5,
  config: {
    name: 'Create studio set',
    parameters: {
      method: 'POST',
      url: `${API}/v1/sets`,
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [
        { name: 'X-Customer-Ref', value: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`) },
        { name: 'X-Channel', value: 'telegram' },
        { name: 'Idempotency-Key', value: expr(`tgset-{{ $('Telegram Trigger').item.json.callback_query.data.split('|').slice(1).join('-') }}`) },
      ] },
      sendBody: true,
      contentType: 'json',
      specifyBody: 'json',
      jsonBody: expr(`{{ JSON.stringify({ group_id: $('Telegram Trigger').item.json.callback_query.data.split('|')[4], mode: $('Telegram Trigger').item.json.callback_query.data.split('|')[1] === 'a' ? 'angles' : 'enhance', style: { ws: 'white_studio', dl: 'dark_luxury', sr: 'showroom' }[$('Telegram Trigger').item.json.callback_query.data.split('|')[2]], hide_plate: $('Telegram Trigger').item.json.callback_query.data.split('|')[3] === '1', consent_version: 'tg-v2' }) }}`),
      options: { response: { response: { fullResponse: true, neverError: true } }, timeout: 30000 },
    },
    credentials: apiCred,
  },
});

const setOk = ifElse({
  version: 2.2,
  config: {
    name: 'Set accepted?',
    parameters: { conditions: {
      options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
      conditions: [{ leftValue: expr('{{ $json.statusCode }}'), rightValue: 202, operator: { type: 'number', operation: 'equals' } }],
      combinator: 'and',
    } },
  },
});

const working = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Say working on it',
    parameters: {
      resource: 'message',
      operation: 'editMessageText',
      messageType: 'message',
      chatId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`),
      messageId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.message_id }}`),
      text: expr(`🎨 Creating {{ $json.body.total_jobs }} <b>{{ { ws: 'White Studio', dl: 'Dark Luxury', sr: 'Showroom' }[$('Telegram Trigger').item.json.callback_query.data.split('|')[2]] }}</b> {{ $('Telegram Trigger').item.json.callback_query.data.split('|')[1] === 'a' ? 'angle view' : 'photo' }}{{ $json.body.total_jobs > 1 ? 's' : '' }}{{ $('Telegram Trigger').item.json.callback_query.data.split('|')[3] === '1' ? ' with the plate blurred' : '' }}…

This usually takes {{ $json.body.total_jobs > 1 ? '1–4 minutes' : '15–60 seconds' }}. I'll send everything here when it's ready.`),
      additionalFields: { parse_mode: 'HTML' },
    },
    credentials: tgCred,
  },
});

const setRejected = node({
  type: 'n8n-nodes-base.telegram',
  version: 1.2,
  config: {
    name: 'Explain set problem',
    parameters: {
      resource: 'message',
      operation: 'editMessageText',
      messageType: 'message',
      chatId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.chat.id }}`),
      messageId: expr(`{{ $('Telegram Trigger').item.json.callback_query.message.message_id }}`),
      text: expr(`😕 {{ $json.body?.error?.message || 'Something went wrong starting your photos. Please send them again.' }}`),
      additionalFields: { parse_mode: 'HTML' },
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

Send me a photo of your car, or an <b>album of up to 6 photos</b> (front, back, sides…), and I'll turn them into studio shots.

You can choose:
✨ <b>Studio-edit</b>: your real photos restyled in a studio
🔄 <b>Extra angles</b>: AI-generated front, back, side and angled views
🔒 Optional <b>number-plate blur</b>

📌 For the sharpest results, send photos <b>as a File</b> (📎 → File) so Telegram doesn't compress them, and use your own camera photos rather than screenshots.

<b>Commands</b>
/status – check your latest photo
/delete – delete all your photos and data
/help – show this message

<i>By sending photos you agree to them being processed by AI to create studio images. Lighting and backgrounds are AI-generated; extra angles are AI visualizations. Photos are deleted after 30 days.</i>`,
      additionalFields: html,
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
      additionalFields: html,
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
      additionalFields: html,
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
      text: 'Send me a photo of your car 📸 (or an album of up to 6) and I\'ll turn it into studio shots. Type /help for more.',
      additionalFields: html,
    },
    credentials: tgCred,
  },
});

const note = sticky(`## CarStudio AI · Telegram bot v2
Photo or album → **what to make** (studio-edit / extra angles) → **style** → **plate blur?** → \`POST /v1/sets\`.
Each step edits the same message, so the chat stays tidy. Only the first photo of an album asks the question.
Results arrive via **Job events → Telegram v2**. Rollback: deactivate this, re-activate v1.`, [], { width: 460, height: 220 });

export default workflow('carstudio-telegram-bot-v2', 'CarStudio · Telegram bot v2')
  .add(tgTrigger)
  .to(route
    .onCase(0, ackMode.to(askStyle))
    .onCase(1, ackStyle.to(askPlate))
    .onCase(2, ackPlate.to(createSet.to(setOk.onTrue(working).onFalse(setRejected))))
    .onCase(3, uploadPhoto.to(uploadOk.onTrue(firstOfAlbum.onTrue(askMode)).onFalse(uploadRejected)))
    .onCase(4, welcome)
    .onCase(5, getStatus.to(sendStatus))
    .onCase(6, deleteData.to(confirmDelete))
    .onCase(7, fallback))
  .add(note)
  .group('Photo → what to make', [uploadPhoto, uploadOk, firstOfAlbum, askMode, uploadRejected], { description: 'Uploads each photo (albums share one group); the first photo of an album asks studio-edit vs extra angles' })
  .group('Mode → style', [ackMode, askStyle], { description: 'Edits the question into the three studio style buttons' })
  .group('Style → plate', [ackStyle, askPlate], { description: 'Asks whether to blur the number plate' })
  .group('Plate → start set', [ackPlate, createSet, setOk, working, setRejected], { description: 'Creates the set via the API and confirms how many images are being made' })
  .group('Status', [getStatus, sendStatus], { description: 'Replies to /status with the latest job state' })
  .group('Delete data', [deleteData, confirmDelete], { description: 'Replies to /delete by erasing the customer\'s photos and data' });
