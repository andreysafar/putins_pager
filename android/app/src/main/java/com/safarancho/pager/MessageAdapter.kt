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

        if (isMine) {
            // Sent = orange bubble, black text
            val sentBg = ContextCompat.getColor(holder.itemView.context, R.color.sent_msg_bg)
            val sentText = ContextCompat.getColor(holder.itemView.context, R.color.sent_msg_text)
            holder.tvText.setTextColor(sentText)
            holder.tvMeta.setTextColor(android.graphics.Color.argb(160, 0, 0, 0))
            holder.container.backgroundTintList =
                android.content.res.ColorStateList.valueOf(sentBg)
        } else {
            // Received = dark green bubble, green text
            val recvBg = ContextCompat.getColor(holder.itemView.context, R.color.received_msg_bg)
            holder.tvText.setTextColor(ContextCompat.getColor(holder.itemView.context, R.color.text_h))
            holder.tvMeta.setTextColor(ContextCompat.getColor(holder.itemView.context, R.color.text_muted))
            holder.container.backgroundTintList =
                android.content.res.ColorStateList.valueOf(recvBg)
        }
    }

    override fun getItemCount() = messages.size
}
