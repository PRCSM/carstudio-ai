// Live workflow id: gpQZzYxDWJhSeR7A — "CarStudio · Job events → Telegram"
import { workflow, node, trigger, sticky, newCredential, ifElse, expr } from '@n8n/workflow-sdk';

const API = 'https://api-production-8ce7.up.railway.app';
const apiCred = { httpHeaderAuth: newCredential('CarStudio API') };
const tgCred = { telegramApi: newCredential('CarStudio Telegram') };

const events = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.2,
  config: {
    name: 'Job events from API',
    parameters: { httpMethod: 'POST', path: 'carstudio-job-events', authentication: 'headerAuth',
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

const note = sticky(`## CarStudio AI · Job events → Telegram
The API's worker posts here when a job finishes (Authorization header checked with the **CarStudio API** credential).
- completed → fetch a fresh 15-min link → send the photo
- needs_more_images / failed / review_required → send a short message`, [], { width: 420, height: 200 });

export default workflow('carstudio-job-events', 'CarStudio · Job events → Telegram')
  .add(events)
  .to(isTelegram.onTrue(hasResult.onTrue(getResult.to(sendPhoto)).onFalse(sendUpdate)))
  .add(note)
  .group('Deliver to Telegram', [hasResult, getResult, sendPhoto, sendUpdate], { description: 'Sends the finished studio photo, or a short update when a retake or review is needed' });
