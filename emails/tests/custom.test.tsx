import test from 'node:test';
import assert from 'node:assert';
import { createElement } from 'react';
import { render } from '@react-email/render';
import Custom from '../templates/custom.tsx';
import { BUSINESS_ADDRESS } from '../components/Layout.tsx';

test('custom template injects html body inside compliance shell', async () => {
  const html = await render(createElement(Custom, {
    preheader: 'Sneak peek',
    htmlBody: '<table><tr><td style="color:#111"><h1>Hand-built design</h1>' +
              '<p>Hey Ada, big news.</p></td></tr></table>',
    unsubUrl: 'https://x.io/u/tok',
  }));
  assert.ok(html.includes('Hand-built design'));
  assert.ok(html.includes('Hey Ada, big news.'));
  assert.ok(html.includes('https://x.io/u/tok'));       // unsub link from shell
  assert.ok(html.includes(BUSINESS_ADDRESS));            // address from shell
  assert.ok(html.toLowerCase().includes('unsubscribe'));
});
