import test from 'node:test';
import assert from 'node:assert';
import { createElement } from 'react';
import { render } from '@react-email/render';
import Newsletter from '../templates/newsletter.tsx';

const props = {
  preheader: 'July updates inside',
  headline: 'The July Launch',
  sections: [
    { heading: 'What is new', paragraphs: ['We shipped a thing.', 'It is good.'] },
    { paragraphs: ['No heading here.'] },
  ],
  cta: { label: 'Read more', url: 'https://growthable.io/launch' },
  firstName: 'Ada',
  unsubUrl: 'https://x.io/u/tok',
};

test('newsletter renders headline, sections, cta, personalization, unsub', async () => {
  const html = await render(createElement(Newsletter, props));
  for (const s of ['The July Launch', 'What is new', 'We shipped a thing.', 'No heading here.',
                   'Read more', 'https://growthable.io/launch', 'Ada', 'https://x.io/u/tok']) {
    assert.ok(html.includes(s), s);
  }
});

test('newsletter renders without optional props', async () => {
  const html = await render(createElement(Newsletter, {
    headline: 'Bare', unsubUrl: 'https://x.io/u/tok',
  }));
  assert.ok(html.includes('Bare'));
});
