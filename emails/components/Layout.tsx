import {
  Body, Container, Head, Hr, Html, Img, Link, Preview, Section, Text,
} from '@react-email/components';
import * as React from 'react';

export const BUSINESS_ADDRESS = '27 Red Ash Drive, Woonona NSW 2517, Australia';

// Growthable brand kit — exact values from the brand SVGs (navy #34475B, pink #F03E6A).
// PNG assets are served by the pipeline's own web service (app/static).
const ASSETS = 'https://growthableemail.onrender.com/assets';
export const BRAND = {
  logoUrl: `${ASSETS}/growthable-logo.png`,            // navy wordmark, transparent bg
  logoWhiteUrl: `${ASSETS}/growthable-logo-white.png`, // white wordmark for dark panels
  iconUrl: `${ASSETS}/growthable-icon.png`,            // standalone "g" device
  navy: '#34475B',        // headings / body text (brand exact)
  navyDark: '#2A3A4C',
  pink: '#F03E6A',        // accent, CTAs, links (brand exact)
  pinkTint: '#FDEEF2',    // soft callout background
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
