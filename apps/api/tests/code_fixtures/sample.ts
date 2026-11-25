import { Router, type Request } from "express";
import * as path from "node:path";
import defaultThing from './thing';
const fs = require("fs");

// function commented(): not a definition
export const MAX_RETRIES = 3;
const url = "http://example.com/{not-a-brace";

export interface Repo<T> {
  get(id: string): Promise<T>;
}

export type Id = string | number;

export enum Color { Red, Green }

export default class UserService extends Base implements Repo<User> {
  private cache = new Map<string, User>();

  constructor(private readonly db: Db) {
    super();
  }

  async get(id: string): Promise<User> {
    if (this.cache.has(id)) {
      return this.cache.get(id)!;
    }
    return this.db.find(`users/${id}`);
  }

  static create(): UserService {
    return new UserService(db());
  }
}

export async function handler(req: Request): Promise<void> {
  const inner = () => 1;
}

export const add = (a: number, b: number): number => a + b;
const mul = async (a, b) => {
  return a * b;
};
