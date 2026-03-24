package com.safarancho.pager

import android.view.Gravity
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView

class MessageAdapter(
    private val messages: List<Message>,
    private val mySS: String
) : RecyclerView.Adapter<MessageAdapter.VH>() {

    class VH(view: android.view.View) : RecyclerView.ViewHolder(view) {
        val container: LinearLayout = view.findViewById(R.id.msgContainer)
        val tvText: TextView = view.findViewById(R.id.tvMsgText)
        val tvMeta: TextView = view.findViewById(R.id.tvMsgMeta)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
        val v = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_message, parent, false)
        return VH(v)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        val m = messages[position]
        holder.tvText.text = m.text
        holder.tvMeta.text = m.created_at.take(16).replace("T", " ")

        val isMine = m.from_ss == mySS
        holder.container.gravity = if (isMine) Gravity.END else Gravity.START

        // Sports-Punk: neon orange for sent, card bg for received
        if (isMine) {
            val orangeColor = ContextCompat.getColor(holder.itemView.context, R.color.accent)
            holder.tvText.setTextColor(ContextCompat.getColor(holder.itemView.context, R.color.bg_dark))
            holder.container.backgroundTintList =
                android.content.res.ColorStateList.valueOf(orangeColor)
            holder.tvMeta.setTextColor(
                ContextCompat.getColor(holder.itemView.context, R.color.bg_dark).let {
                    android.graphics.Color.argb(150, android.graphics.Color.red(it),
                        android.graphics.Color.green(it), android.graphics.Color.blue(it))
                }
            )
        } else {
            val bgColor = ContextCompat.getColor(holder.itemView.context, R.color.bg_card)
            holder.tvText.setTextColor(ContextCompat.getColor(holder.itemView.context, R.color.text_h))
            holder.container.backgroundTintList =
                android.content.res.ColorStateList.valueOf(bgColor)
            holder.tvMeta.setTextColor(
                ContextCompat.getColor(holder.itemView.context, R.color.text_muted)
            )
        }
    }

    override fun getItemCount() = messages.size
}
