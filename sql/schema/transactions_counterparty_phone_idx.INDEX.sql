-- Name: transactions_counterparty_phone_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX transactions_counterparty_phone_idx ON public.transactions USING btree (counterparty_phone);


--
