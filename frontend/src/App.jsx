import React from 'react'
import VoiceBot from './VoiceBot.jsx'

export default function App() {
  return (
    <div className="min-h-screen bg-gray-900 flex flex-col items-center justify-start py-8 px-4">
      <div className="w-full max-w-2xl">
        <VoiceBot />
      </div>
      <footer className="mt-8 text-gray-500 text-sm text-center">
        Powered by{' '}
        <a
          href="https://sarvam.ai"
          target="_blank"
          rel="noopener noreferrer"
          className="text-red-400 hover:text-red-300 font-medium transition-colors"
        >
          Sarvam AI
        </a>
        {' '}• Airtel Customer Support Bot
      </footer>
    </div>
  )
}
