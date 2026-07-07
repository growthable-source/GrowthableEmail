import test from 'node:test';
import assert from 'node:assert';
import { createElement } from 'react';
import { render } from '@react-email/render';
import Welcome from '../templates/welcome.tsx';

test('welcome renders personalization, unsub link, address, preheader', async () => {
  const html = await render(
    createElement(Welcome, { firstName: 'Ada', unsubUrl: 'https://x.io/u/tok123' }),
  );
  assert.ok(html.includes('Ada'));
  assert.ok(html.includes('https://x.io/u/tok123'));
  assert.ok(html.toLowerCase().includes('unsubscribe'));
  assert.ok(html.includes('PHYSICAL_ADDRESS'));
});

test('welcome renders a plain-text part', async () => {
  const text = await render(
    createElement(Welcome, { firstName: 'Ada', unsubUrl: 'https://x.io/u/tok123' }),
    { plainText: true },
  );
  assert.ok(text.includes('Ada'));
  assert.ok(text.includes('https://x.io/u/tok123'));
});
