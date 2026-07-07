import {
  Body, Container, Head, Hr, Html, Link, Preview, Section, Text,
} from '@react-email/components';
import * as React from 'react';

export const BUSINESS_ADDRESS = '27 Red Ash Drive, Woonona NSW 2517, Australia';

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
      <Body style={{ backgroundColor: '#f4f4f5', fontFamily: 'Helvetica, Arial, sans-serif' }}>
        <Container style={{ backgroundColor: '#ffffff', margin: '0 auto', padding: '32px', maxWidth: '600px' }}>
          <Text style={{ fontSize: '20px', fontWeight: 700, color: '#18181b' }}>Growthable</Text>
          <Section>{children}</Section>
          <Hr style={{ borderColor: '#e4e4e7', margin: '32px 0 16px' }} />
          <Text style={{ fontSize: '12px', color: '#71717a', lineHeight: '18px' }}>
            {BUSINESS_ADDRESS}
            <br />
            You are receiving this because you are a Growthable contact.{' '}
            <Link href={unsubUrl} style={{ color: '#71717a', textDecoration: 'underline' }}>
              Unsubscribe
            </Link>
          </Text>
        </Container>
      </Body>
    </Html>
  );
}
