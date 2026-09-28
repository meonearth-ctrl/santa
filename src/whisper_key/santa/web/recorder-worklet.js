// recorder-worklet.js — runs on the audio thread. Copies each 128-sample
// block of the first input channel to the main thread, where it is buffered,
// metered, and finally encoded as 16 kHz mono WAV.
class SantaRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length) {
      this.port.postMessage(input[0].slice(0));
    }
    return true;
  }
}
registerProcessor('santa-recorder', SantaRecorder);
