-- Name: transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transactions (
    ref text NOT NULL,
    counterparty_name text NOT NULL,
    counterparty_phone text,
    phone_source text,
    direction text,
    amount numeric(14,2),
    currency text DEFAULT 'ILS'::text NOT NULL,
    note text,
    status text,
    funding text,
    tx_at timestamp with time zone,
    list_row jsonb DEFAULT '{}'::jsonb NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);


--
