import { createElement } from 'react';
import { render } from '@react-email/render';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  const input = JSON.parse(await readStdin());
  if (!input.template || !Array.isArray(input.props)) {
    throw new Error('expected {template: string, props: object[]}');
  }
  if (!/^[\w-]+$/.test(input.template)) {
    throw new Error(`invalid template ref: ${input.template}`);
  }
  const templatePath = path.join(
    path.dirname(new URL(import.meta.url).pathname), 'templates', `${input.template}.tsx`,
  );
  const mod = await import(pathToFileURL(templatePath).href);
  const Component = mod.default;
  const out = [];
  for (const props of input.props) {
    out.push({
      html: await render(createElement(Component, props)),
      text: await render(createElement(Component, props), { plainText: true }),
    });
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch((err) => {
  process.stderr.write(String(err?.stack ?? err));
  process.exit(1);
});
