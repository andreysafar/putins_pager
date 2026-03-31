package com.mesh.pager

import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.animation.AccelerateDecelerateInterpolator
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView

class ContactAdapter(
    private val contacts: List<Contact>,
    private val unreadCounts: Map<String, Int>,
    private val onClick: (Contact) -> Unit
) : RecyclerView.Adapter<ContactAdapter.VH>() {

    class VH(view: View) : RecyclerView.ViewHolder(view) {
        val tvName: TextView = view.findViewById(R.id.tvContactName)
        val tvSS: TextView = view.findViewById(R.id.tvContactSS)
        val badgeDot: View = view.findViewById(R.id.badgeDot)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
        val v = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_contact, parent, false)
        return VH(v)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        val c = contacts[position]
        holder.tvName.text = c.display_name
        holder.tvSS.text = c.ss_id

        val count = unreadCounts[c.ss_id] ?: 0
        holder.badgeDot.isActivated = count > 0

        // Sports-Punk glow animation on badge when unread
        if (count > 0) {
            val animator = ObjectAnimator.ofFloat(holder.badgeDot, "alpha", 0.4f, 1f).apply {
                duration = 900
                repeatMode = ValueAnimator.REVERSE
                repeatCount = ValueAnimator.INFINITE
                interpolator = AccelerateDecelerateInterpolator()
            }
            holder.badgeDot.tag = animator  // Store for cleanup
            animator.start()
        } else {
            (holder.badgeDot.tag as? ObjectAnimator)?.cancel()
            holder.badgeDot.alpha = 1f
        }

        holder.itemView.setOnClickListener { onClick(c) }
    }

    override fun onViewRecycled(holder: VH) {
        super.onViewRecycled(holder)
        (holder.badgeDot.tag as? ObjectAnimator)?.cancel()
    }

    override fun getItemCount() = contacts.size
}
