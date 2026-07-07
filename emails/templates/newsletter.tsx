import { Button, Heading, Text } from '@react-email/components';
import * as React from 'react';
import Layout from '../components/Layout.tsx';

interface Section { heading?: string; paragraphs: string[]; }
interface NewsletterProps {
  preheader?: string;
  headline: string;
  sections?: Section[];
  cta?: { label: string; url: string };
  firstName?: string;
  unsubUrl: string;
}

const bodyStyle = { fontSize: '16px', color: '#18181b', lineHeight: '24px' };

export default function Newsletter({
  preheader, headline, sections = [], cta, firstName, unsubUrl,
}: NewsletterProps) {
  return (
    <Layout preheader={preheader || headline} unsubUrl={unsubUrl}>
      <Heading as="h1" style={{ fontSize: '24px', color: '#18181b', lineHeight: '32px' }}>
        {headline}
      </Heading>
      {firstName ? <Text style={bodyStyle}>Hi {firstName},</Text> : null}
      {sections.map((section, i) => (
        <React.Fragment key={i}>
          {section.heading ? (
            <Heading as="h2" style={{ fontSize: '18px', color: '#18181b', lineHeight: '26px' }}>
              {section.heading}
            </Heading>
          ) : null}
          {section.paragraphs.map((p, j) => <Text key={j} style={bodyStyle}>{p}</Text>)}
        </React.Fragment>
      ))}
      {cta ? (
        <Button href={cta.url}
          style={{ backgroundColor: '#18181b', color: '#ffffff', padding: '12px 20px', borderRadius: '6px', fontSize: '14px' }}>
          {cta.label}
        </Button>
      ) : null}
    </Layout>
  );
}
