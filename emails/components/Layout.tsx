import {
  Body, Container, Head, Hr, Html, Img, Link, Preview, Section, Text,
} from '@react-email/components';
import * as React from 'react';

export const BUSINESS_ADDRESS = '27 Red Ash Drive, Woonona NSW 2517, Australia';

// Growthable brand kit — derived from the logo (navy wordmark, pink arrow accent).
export const BRAND = {
  logoUrl: 'https://growthable.io/brand/logo.jpg',
  navy: '#38445B',        // headings / body text
  navyDark: '#2E3A4F',
  pink: '#EF4B6A',        // accent, CTAs, links
  pinkTint: '#FDEFF2',    // soft callout background
  cream: '#F7F5F2',       // email canvas
  cardBorder: '#E8EAEE',
  grey: '#7C8494',        // secondary text
};

interface LayoutProps {
  preheader: string;
  unsubUrl: string;
  children: React.ReactNode;
}

export default function Layout({ preheader, unsubUrl, children }: LayoutProps) {
  return (
    <Html lang="en">
      <Head />
      <Preview>{preheader}</Preview>
      <Body style={{ backgroundColor: BRAND.cream, fontFamily: "'Helvetica Neue', Helvetica, Arial, sans-serif", margin: 0, padding: '24px 0' }}>
        <Container style={{ backgroundColor: '#ffffff', margin: '0 auto', padding: '32px', maxWidth: '600px', borderRadius: '16px', border: `1px solid ${BRAND.cardBorder}` }}>
          <Img src={BRAND.logoUrl} alt="Growthable" width="170" style={{ marginBottom: '24px' }} />
          <Section>{children}</Section>
          <Hr style={{ borderColor: BRAND.cardBorder, margin: '32px 0 16px' }} />
          <Text style={{ fontSize: '12px', color: BRAND.grey, lineHeight: '18px' }}>
            {BUSINESS_ADDRESS}
            <br />
            You are receiving this because you are a Growthable contact.{' '}
            <Link href={unsubUrl} style={{ color: BRAND.grey, textDecoration: 'underline' }}>
              Unsubscribe
            </Link>
          </Text>
        </Container>
      </Body>
    </Html>
  );
}
