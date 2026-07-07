import { Button, Text } from '@react-email/components';
import * as React from 'react';
import Layout from '../components/Layout.tsx';

interface WelcomeProps {
  firstName?: string;
  unsubUrl: string;
}

export default function Welcome({ firstName = 'there', unsubUrl }: WelcomeProps) {
  return (
    <Layout preheader="News and updates from the Growthable team" unsubUrl={unsubUrl}>
      <Text style={{ fontSize: '16px', color: '#18181b', lineHeight: '24px' }}>
        Hi {firstName},
      </Text>
      <Text style={{ fontSize: '16px', color: '#18181b', lineHeight: '24px' }}>
        Welcome to the new Growthable newsletter. Expect practical updates — no fluff.
      </Text>
      <Button
        href="https://growthable.io"
        style={{ backgroundColor: '#18181b', color: '#ffffff', padding: '12px 20px', borderRadius: '6px', fontSize: '14px' }}
      >
        Visit Growthable
      </Button>
    </Layout>
  );
}
