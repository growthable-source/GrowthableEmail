import * as React from 'react';
import Layout from '../components/Layout.tsx';

// Bot-authored campaigns: the AI designs the body HTML freely; the Layout shell
// guarantees the compliance chrome (unsubscribe link, physical address, preheader).
interface CustomProps {
  preheader?: string;
  htmlBody: string;
  unsubUrl: string;
}

export default function Custom({ preheader = '', htmlBody, unsubUrl }: CustomProps) {
  return (
    <Layout preheader={preheader} unsubUrl={unsubUrl}>
      <div dangerouslySetInnerHTML={{ __html: htmlBody }} />
    </Layout>
  );
}
