import { Button, Heading, Text } from '@react-email/components';
import * as React from 'react';
import Layout, { BRAND } from '../components/Layout.tsx';

interface Section { heading?: string; paragraphs: string[]; }
interface NewsletterProps {
  preheader?: string;
  headline: string;
  sections?: Section[];
  cta?: { label: string; url: string };
  firstName?: string;
  unsubUrl: string;
}

const bodyStyle = { fontSize: '16px', color: BRAND.navy, lineHeight: '25px' };

export default function Newsletter({
  preheader, headline, sections = [], cta, firstName, unsubUrl,
}: NewsletterProps) {
  return (
    <Layout preheader={preheader || headline} unsubUrl={unsubUrl}>
      <Heading as="h1" style={{ fontSize: '26px', color: BRAND.navyDark, lineHeight: '34px', fontWeight: 700 }}>
        {headline}
      </Heading>
      {firstName ? <Text style={bodyStyle}>Hi {firstName},</Text> : null}
      {sections.map((section, i) => (
        <React.Fragment key={i}>
          {section.heading ? (
            <Heading as="h2" style={{ fontSize: '18px', color: BRAND.navyDark, lineHeight: '26px' }}>
              {section.heading}
            </Heading>
          ) : null}
          {section.paragraphs.map((p, j) => <Text key={j} style={bodyStyle}>{p}</Text>)}
        </React.Fragment>
      ))}
      {cta ? (
        <Button href={cta.url}
          style={{ backgroundColor: BRAND.pink, color: '#ffffff', padding: '13px 28px', borderRadius: '999px', fontSize: '15px', fontWeight: 600 }}>
          {cta.label}
        </Button>
      ) : null}
    </Layout>
  );
}
