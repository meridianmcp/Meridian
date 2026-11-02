/**
 * An error whose message is written for the caller and is safe to return to them.
 *
 * server.ts sends an ExpectedError's message back to the extension. Any other
 * error is logged to this process's stderr and answered with a fixed generic
 * message, so internal paths and stack details stay out of HTTP responses.
 */
export class ExpectedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ExpectedError";
  }
}
